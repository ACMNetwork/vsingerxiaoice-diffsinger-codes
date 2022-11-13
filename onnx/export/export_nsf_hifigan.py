import json
import os
import sys

import numpy as np
import onnx
import onnxsim
import torch
import torch.nn as nn
import torch.nn.functional as functional
from torch.nn import Conv1d, ConvTranspose1d, Conv2d
from torch.nn.utils import weight_norm, remove_weight_norm

from modules.nsf_hifigan.env import AttrDict
from modules.nsf_hifigan.models import ResBlock1, ResBlock2
from modules.nsf_hifigan.utils import init_weights
from utils.hparams import set_hparams, hparams

LRELU_SLOPE = 0.1


class SineGen(torch.nn.Module):
    """ Definition of sine generator
    SineGen(samp_rate, harmonic_num = 0,
            sine_amp = 0.1, noise_std = 0.003,
            voiced_threshold = 0,
            flag_for_pulse=False)
    samp_rate: sampling rate in Hz
    harmonic_num: number of harmonic overtones (default 0)
    sine_amp: amplitude of sine-wavefrom (default 0.1)
    noise_std: std of Gaussian noise (default 0.003)
    voiced_thoreshold: F0 threshold for U/V classification (default 0)
    flag_for_pulse: this SinGen is used inside PulseGen (default False)
    Note: when flag_for_pulse is True, the first time step of a voiced
        segment is always sin(np.pi) or cos(0)
    """

    def __init__(self, samp_rate, harmonic_num=0,
                 sine_amp=0.1, noise_std=0.003,
                 voiced_threshold=0):
        super(SineGen, self).__init__()
        self.sine_amp = sine_amp
        self.noise_std = noise_std
        self.harmonic_num = harmonic_num
        self.dim = self.harmonic_num + 1
        self.sampling_rate = samp_rate
        self.voiced_threshold = voiced_threshold
        self.diff = Conv2d(
            in_channels=1,
            out_channels=1,
            kernel_size=(2, 1),
            stride=(1, 1),
            padding=0,
            dilation=(1, 1),
            bias=False
        )
        self.diff.weight = nn.Parameter(torch.FloatTensor([[[[-1.], [1.]]]]))

    def _f02sine(self, f0_values):
        """ f0_values: (batchsize, length, dim)
            where dim indicates fundamental tone and overtones
        """
        # convert to F0 in rad. The integer part n can be ignored
        # because 2 * np.pi * n doesn't affect phase
        rad_values = (f0_values / self.sampling_rate).fmod(1.)

        # initial phase noise (no noise for fundamental component)
        rand_ini = torch.rand(1, self.dim, device=f0_values.device)
        rand_ini[:, 0] = 0
        rad_values[:, 0, :] += rand_ini

        # instantanouse phase sine[t] = sin(2*pi \sum_i=1 ^{t} rad)

        # To prevent torch.cumsum numerical overflow,
        # it is necessary to add -1 whenever \sum_k=1^n rad_value_k > 1.
        # Buffer tmp_over_one_idx indicates the time step to add -1.
        # This will not change F0 of sine because (x-1) * 2*pi = x * 2*pi
        tmp_over_one = torch.cumsum(rad_values, dim=1).fmod(1.)

        diff = self.diff(tmp_over_one.unsqueeze(1)).squeeze(1)  # Equivalent to torch.diff, but able to export ONNX
        cumsum_shift = (diff < 0).float()
        cumsum_shift = torch.cat((torch.zeros((1, 1, self.dim)).to(f0_values.device), cumsum_shift), dim=1)
        sines = torch.sin(torch.cumsum(rad_values - cumsum_shift, dim=1) * (2 * np.pi))
        return sines

    def forward(self, f0):
        """ sine_tensor, uv = forward(f0)
        input F0: tensor(batchsize=1, length, dim=1)
                  f0 for unvoiced steps should be 0
        output sine_tensor: tensor(batchsize=1, length, dim)
        output uv: tensor(batchsize=1, length, 1)
        """
        with torch.no_grad():
            # fundamental component
            fn = torch.multiply(f0, torch.FloatTensor([[range(1, self.harmonic_num + 2)]]).to(f0.device))

            # generate sine waveforms
            sine_waves = self._f02sine(fn) * self.sine_amp

            # generate uv signal
            uv = (f0 > self.voiced_threshold).float()

            # noise: for unvoiced should be similar to sine_amp
            #        std = self.sine_amp/3 -> max value ~ self.sine_amp
            # .       for voiced regions is self.noise_std
            noise_amp = uv * self.noise_std + (1 - uv) * (self.sine_amp / 3)
            noise = noise_amp * torch.randn_like(sine_waves)

            # first: set the unvoiced part to 0 by uv
            # then: additive noise
            sine_waves = sine_waves * uv + noise
        return sine_waves, uv, noise


class SourceModuleHnNSF(torch.nn.Module):
    """ SourceModule for hn-nsf
    SourceModule(sampling_rate, harmonic_num=0, sine_amp=0.1,
                 add_noise_std=0.003, voiced_threshod=0)
    sampling_rate: sampling_rate in Hz
    harmonic_num: number of harmonic above F0 (default: 0)
    sine_amp: amplitude of sine source signal (default: 0.1)
    add_noise_std: std of additive Gaussian noise (default: 0.003)
        note that amplitude of noise in unvoiced is decided
        by sine_amp
    voiced_threshold: threhold to set U/V given F0 (default: 0)
    Sine_source, noise_source = SourceModuleHnNSF(F0_sampled)
    F0_sampled (batchsize, length, 1)
    Sine_source (batchsize, length, 1)
    noise_source (batchsize, length 1)
    uv (batchsize, length, 1)
    """

    def __init__(self, sampling_rate, harmonic_num=0, sine_amp=0.1,
                 add_noise_std=0.003, voiced_threshod=0):
        super(SourceModuleHnNSF, self).__init__()

        self.sine_amp = sine_amp
        self.noise_std = add_noise_std

        # to produce sine waveforms
        self.l_sin_gen = SineGen(sampling_rate, harmonic_num, sine_amp, add_noise_std, voiced_threshod)

        # to merge source harmonics into a single excitation
        self.l_linear = torch.nn.Linear(harmonic_num + 1, 1)
        self.l_tanh = torch.nn.Tanh()

    def forward(self, x):
        """
        Sine_source, noise_source = SourceModuleHnNSF(F0_sampled)
        F0_sampled (batchsize, length, 1)
        Sine_source (batchsize, length, 1)
        noise_source (batchsize, length 1)
        """
        # source for harmonic branch
        sine_wavs, uv, _ = self.l_sin_gen(x)
        sine_merge = self.l_tanh(self.l_linear(sine_wavs))

        # source for noise branch, in the same shape as uv
        noise = torch.randn_like(uv) * (self.sine_amp / 3)
        return sine_merge, noise, uv


class Generator(torch.nn.Module):
    def __init__(self, h):
        super(Generator, self).__init__()
        self.h = h
        self.num_kernels = len(h.resblock_kernel_sizes)
        self.num_upsamples = len(h.upsample_rates)
        self.f0_upsamp = torch.nn.Upsample(scale_factor=float(np.prod(h.upsample_rates)))
        self.m_source = SourceModuleHnNSF(
            sampling_rate=h.sampling_rate,
            harmonic_num=8)
        self.noise_convs = nn.ModuleList()
        self.conv_pre = weight_norm(Conv1d(h.num_mels, h.upsample_initial_channel, 7, 1, padding=3))
        resblock = ResBlock1 if h.resblock == '1' else ResBlock2

        self.ups = nn.ModuleList()
        for i, (u, k) in enumerate(zip(h.upsample_rates, h.upsample_kernel_sizes)):
            c_cur = h.upsample_initial_channel // (2 ** (i + 1))
            self.ups.append(weight_norm(
                ConvTranspose1d(h.upsample_initial_channel // (2 ** i), h.upsample_initial_channel // (2 ** (i + 1)),
                                k, u, padding=(k - u) // 2)))
            if i + 1 < len(h.upsample_rates):  #
                stride_f0 = np.prod(h.upsample_rates[i + 1:])
                self.noise_convs.append(Conv1d(
                    1, c_cur, kernel_size=stride_f0 * 2, stride=int(stride_f0), padding=stride_f0 // 2))
            else:
                self.noise_convs.append(Conv1d(1, c_cur, kernel_size=1))
        self.resblocks = nn.ModuleList()
        ch = None
        for i in range(len(self.ups)):
            ch = h.upsample_initial_channel // (2 ** (i + 1))
            for j, (k, d) in enumerate(zip(h.resblock_kernel_sizes, h.resblock_dilation_sizes)):
                self.resblocks.append(resblock(h, ch, k, d))

        self.conv_post = weight_norm(Conv1d(ch, 1, 7, 1, padding=3))
        self.ups.apply(init_weights)
        self.conv_post.apply(init_weights)

    def forward(self, x, f0):
        f0 = self.f0_upsamp(f0.unsqueeze(1)).transpose(1, 2)  # bs,n,t
        har_source, noi_source, uv = self.m_source(f0)
        har_source = har_source.transpose(1, 2)
        x = self.conv_pre(x)

        for i in range(self.num_upsamples):
            x = functional.leaky_relu(x, LRELU_SLOPE)

            x = self.ups[i](x)
            x_source = self.noise_convs[i](har_source)

            x = x + x_source
            xs = None
            for j in range(self.num_kernels):
                if xs is None:
                    xs = self.resblocks[i * self.num_kernels + j](x)
                else:
                    xs += self.resblocks[i * self.num_kernels + j](x)
            x = xs / self.num_kernels
        x = functional.leaky_relu(x)
        x = self.conv_post(x)
        x = torch.tanh(x)

        x = x.squeeze(1)
        return x

    def remove_weight_norm(self):
        print('Removing weight norm...')
        for up in self.ups:
            remove_weight_norm(up)
        for block in self.resblocks:
            block.remove_weight_norm()
        remove_weight_norm(self.conv_pre)
        remove_weight_norm(self.conv_post)


class NsfHiFiGAN(torch.nn.Module):
    def __init__(self, device=None):
        super().__init__()
        if device is None:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.device = device
        self.generator, self.hparams = load_model(hparams['vocoder_ckpt'], device)

    def forward(self, mel: torch.Tensor, f0: torch.Tensor):
        mel = mel.transpose(2, 1) * 2.30259
        wav = self.generator.forward(mel, f0)
        return wav


def load_model(model_path, device):
    config_file = os.path.join(os.path.split(model_path)[0], 'config.json')
    with open(config_file) as f:
        data = f.read()

    json_config = json.loads(data)
    h = AttrDict(json_config)

    generator = Generator(h).to(device)

    cp_dict = torch.load(model_path)
    generator.load_state_dict(cp_dict['generator'], strict=False)
    generator.eval()
    generator.remove_weight_norm()
    del cp_dict
    return generator, h


def simplify(src, target):
    model = onnx.load(src)

    in_dims = model.graph.input[0].type.tensor_type.shape.dim
    outputs = model.graph.output
    new_output = onnx.helper.make_value_info(
        name=outputs[0].name,
        type_proto=onnx.helper.make_tensor_type_proto(
            elem_type=onnx.TensorProto.FLOAT,
            shape=(in_dims[0].dim_value, 'n_samples')
        )
    )
    outputs.remove(outputs[0])
    outputs.insert(0, new_output)
    print(f'Fix output: \'{model.graph.output[0].name}\'')

    model, check = onnxsim.simplify(model, include_subgraph=True)
    assert check, 'Simplified ONNX model could not be validated'

    onnx.save(model, target)


def export(path):
    set_hparams(print_hparams=True)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    vocoder = NsfHiFiGAN(device)
    n_frames = 10

    with torch.no_grad():
        mel = torch.rand((1, n_frames, 128), device=device)
        f0 = torch.rand((1, n_frames), device=device)
        torch.onnx.export(
            vocoder,
            (
                mel,
                f0
            ),
            path,
            input_names=[
                'mel',
                'f0'
            ],
            output_names=[
                'waveform'
            ],
            dynamic_axes={
                'mel': {
                    1: 'n_frames',
                },
                'f0': {
                    1: 'n_frames'
                }
            },
            opset_version=11
        )


if __name__ == '__main__':
    sys.argv = [
        'inference/svs/ds_e2e.py',
        '--config',
        'configs/midi/cascade/opencs/test.yaml',
    ]
    export('onnx/assets/nsf_hifigan.onnx')
    simplify('onnx/assets/nsf_hifigan.onnx', 'onnx/assets/nsf_hifigan.onnx')
