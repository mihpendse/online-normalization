# -*- coding: utf-8 -*-
"""
Released under BSD 3-Clause License, 
Copyright (c) 2019 Cerebras Systems Inc.
All rights reserved.

This module implements the Online Normalization algorithm and the components
which go into it.
"""
import warnings

import torch
import torch.nn as nn


class LayerScaling(nn.Module):
    r"""Scales inputs by the second moment for the entire layer.
    .. math::

        y = \frac{x}{\sqrt{\mathrm{E}[x^2] + \epsilon}}

    Args:
        eps: a value added to the denominator for numerical stability.
            Default: 1e-5

    Shape:
        - Input: :math:`(N, C, H, W)`
        - Output: :math:`(N, C, H, W)` (same shape as input)

    Examples::

        >>> ls = LayerScaling()
        >>> input = torch.randn(64, 128, 32, 32)
        >>> output = ls(input)

    """
    def __init__(self, eps=1e-5, **kwargs):
        super(LayerScaling, self).__init__()
        self.eps = eps

    def extra_repr(self):
        return f'eps={self.eps}'

    def forward(self, input):
        # calculate second moment
        rank = input.dim()
        tmp = input.view(input.size(0), -1)
        moment2 = torch.mean(tmp * tmp, dim=1, keepdim=True)
        for _ in range(rank - 2): moment2 = moment2.unsqueeze(-1)
        # divide out second moment
        return input / torch.sqrt(moment2 + self.eps)


class ControlNorm2DLoop(nn.Module):
    r"""Applies Control Normalization (the per-channel exponential moving
    average, ema, forward and control process backward part of the Online
    Normalization algorithm) over a 4D input (a mini-batch of 3D inputs) as
    described in the paper:
    `Online Normalization for Training Neural Networks`.

    .. math::
        y_t = \frac{x_t - \mu_{t-1}}{\sqrt{\sigma^2_{t-1} + \epsilon}}

        \sigma^2_t = \alpha_fwd * \sigma^2_{t-1} + \alpha_fwd * (1 - \alpha_fwd) * (x_t - \mu_{t-1}) ^ 2
        \mu_t = \alpha_fwd * \mu_{t-1} + (1 - \alpha_fwd) * x_t

    The mean and standard-deviation are estimated per-channel

    Args:
        num_features: :math:`L` from an expected input of size :math:`(N, L)`
        eps: a value added to the denominator for numerical stability.
            Default: 1e-5
        alpha_fwd: the decay factor to be used in fprop to update statistics.
            Default: 1000
        alpha_bkw: the decay factor to be used in fprop to control the
            gradients propagating through the network. Default: 100

    Shape:
        - Input: :math:`(N, C, H, W)`
        - Output: :math:`(N, C, H, W)` (same shape as input)

    Examples::

        >>> norm = ControlNorm2DLoop(128, 0.999, 0.99)
        >>> input = torch.randn(64, 128, 32, 32)
        >>> output = norm(input)
    """

    __constants__ = ['m', 'var', 'u', 'v', 'afwd', 'abkw', 'eps']

    def __init__(self, num_features,
                 alpha_fwd=0.999, alpha_bkw=0.99, eps=1e-05, **kwargs):
        super(ControlNorm2DLoop, self).__init__()
        self.num_features = num_features
        self.eps = eps

        self.afwd = alpha_fwd
        self.abkw = alpha_bkw

        # self.m and self.var are the streaming mean and variance respectively
        self.register_buffer('m', torch.zeros([num_features]))
        self.register_buffer('var', torch.ones([num_features]))

        # self.u and self.v are the control variables respectively
        self.register_buffer('u', torch.zeros([num_features]))
        self.register_buffer('v', torch.zeros([num_features]))
        self.init_norm_params()

        class ControlNormalization(torch.autograd.Function):
            @staticmethod
            def forward(ctx, input):
                afwd = self.afwd
                out = torch.empty_like(input)
                scale = torch.empty_like(input[:, :, 0, 0])

                mu, var = self.moments(input)

                for idx in range(input.size(0)):
                    # fprop activations
                    scale[idx] = torch.sqrt(self.var + self.eps).clone()
                    _mu = self.m.unsqueeze(-1).unsqueeze(-1)
                    _stddev = scale[idx].unsqueeze(-1).unsqueeze(-1)
                    out[idx] = (input[idx] - _mu) / _stddev

                    # Update statistics trackers
                    self.var.data = (afwd * self.var +
                                     (1 - afwd) * var[idx] +
                                     (afwd * (1 - afwd) *
                                      (mu[idx] - self.m) ** 2))
                    self.m.data.add_((1 - afwd) * (mu[idx] - self.m))

                # save for backwards
                ctx.save_for_backward(out.clone(), scale.clone())
                return out

            @staticmethod
            def backward(ctx, grad_out):
                out, scale, = ctx.saved_tensors
                abkw = self.abkw
                grad_in = torch.empty_like(grad_out)

                for idx in range(grad_out.size(0)):
                    # ctrl grad_out with v controller
                    grad_v_ctrl = (grad_out[idx] -
                                   (1 - abkw) * self.v.unsqueeze(-1).unsqueeze(-1) * out[idx])

                    # update v control variable
                    self.v.data.add_(self.mean(grad_v_ctrl * out[idx]))

                    # scale delta
                    grad_scaled = grad_v_ctrl / scale[idx].unsqueeze(-1).unsqueeze(-1)

                    # ctrl grad_scaled with u controller
                    grad_in[idx] = grad_scaled - (1 - abkw) * self.u.unsqueeze(-1).unsqueeze(-1)

                    # Update control variables
                    self.u.data.add_(self.mean(grad_in[idx]))

                return grad_in

        self.normalizer = ControlNormalization.apply

    def init_norm_params(self):
        nn.init.constant_(self.m, 0)
        nn.init.constant_(self.var, 1)
        nn.init.constant_(self.u, 0)
        nn.init.constant_(self.v, 0)

    def moments(self, inputs):
        n = inputs.size(2) * inputs.size(3)

        mu = torch.sum(inputs, dim=(2, 3), keepdim=True) / n
        mu0 = inputs - mu
        return (mu.squeeze(),
                torch.sum(mu0 * mu0, dim=(2, 3), keepdim=False) / n)

    def mean(self, inputs, dim=(1, 2)):
        n = inputs.size(dim[0]) * inputs.size(dim[1])
        return torch.sum(inputs, dim=dim, keepdim=False) / n

    def extra_repr(self):
        s = (f'num_features={self.num_features}, afwd={self.afwd}, '
             f'abkw={self.abkw}, eps={self.eps}')
        return s

    def forward(self, input):
        if self.training:
            return self.normalizer(input)
        mu = self.m.unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        var = self.var.unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        return (input - mu) / torch.sqrt(var + self.eps)


def lin_momentum(mu_prev, mu_curr, mu_stream,
                 momentum, momentum_pow, momentum_batch):
    """
    Helper function for performing an exponential moving average, ema, using
    convolutions which can be distributed across compute fabric.

    Arguments:
        mu_prev: previous time steps statistic
        mu_curr: this time steps statistic
        mu_stream: ema from last time step
        momentum: decay factor of streaming process
        momentum_pow: momentum ^ range(b_size - 1, -1, -1)
        momentum_batch: momentum ^ b_size of size :math:`(N, L)`

    Return:
        updated mu_stream (stale): to use for fprop
        updated mu_stream (current): to cache for next iteration
    """
    input = torch.cat([mu_prev[1:], mu_curr]).transpose(0, 1).unsqueeze(1)
    tmp = torch.nn.functional.conv1d(input, momentum_pow).squeeze().transpose(0, 1)
    curr = (momentum_batch * mu_stream + (1 - momentum) * tmp)

    return torch.cat([mu_stream[-1].unsqueeze(0), curr[:-1]]), curr


def conv_alongb_w1(input, b, c):
    """
    helper functions for fast lin_crtls
    Convolve along 2b dimension with a b length vector of 1's

    assumes order (b, 2b, c)
    """
    input = input.transpose(0, 1).clone().view(2 * b, -1).t().unsqueeze(1)
    tmp = torch.nn.functional.conv1d(input, torch.ones_like(input[0:1, 0:1, :b]))
    return tmp.squeeze().t().view(b + 1, b, c).clone().transpose(0, 1)


def mean_tensor(input, norm_ax, keepdim=True):
    """
    outputs the mean of a pytorch tensor object along axis
    """
    assert isinstance(norm_ax, (tuple, int)), f'norm_ax must be a tuple or int, {norm_ax} is a {type(norm_ax)}'
    dims = input.size()

    if isinstance(norm_ax, int):
        n = dims[norm_ax]
    else:
        n = 1
        for d in norm_ax: n *= dims[d]

    mu = torch.sum(input, dim=norm_ax, keepdim=keepdim) / n

    return mu


def lin_crtl(delta, out, b_size, num_features, v_p, alpha_p, beta_p,
             abkw, eps=1e-32):
    """
    Helper function for controlling with v controller using
    convolutions which can be distributed across compute fabric.

    Arguments:
        delta: grad_out
        out: output of normalization
        b_size: batch size
        num_features: number of features (L)
        v_p: the previous time steps v estimate
        alpha_p: the previous time steps alpha estimate
        beta_p: the previous time steps beta estimate
        abkw: decay factor for bpass
        eps: eps by which to clip alpha which gets log applied to it.
            Default: 1e-32

    Return:
        grad_in
        v_new: current estimate of v
        alpha_p: current estimate of alpha
        beta_p: current estimate of beta
    """

    # expect 0 << alpha ~<1 so we can move it to log space
    alpha = (torch.ones_like(delta[:, :, 0, 0]) -
             (1 - abkw) * mean_tensor(out * out,
                                      norm_ax=(2, 3),
                                      keepdim=True).view(b_size,
                                                         num_features))
    alpha = torch.clamp(alpha, min=eps)
    beta = mean_tensor(delta * out,
                       norm_ax=(2, 3), keepdim=True).view(b_size, num_features)

    alpha2log = torch.log(torch.cat((alpha_p, alpha), 0))
    beta2 = torch.cat((beta_p, beta), 0)

    alpha2logcir = alpha2log.repeat(2 * b_size + 1, 1).view(2 * b_size,
                                                            2 * b_size + 1,
                                                            num_features)[1:b_size + 1,
                                                                          :b_size]
    alpha2logcir2 = torch.cat((alpha2logcir,
                               torch.zeros_like(alpha2logcir)), 1)
    alpha2logcir2conv = conv_alongb_w1(alpha2logcir2, b_size, num_features)
    weight_d = torch.exp(alpha2logcir2conv)

    beta2cir = beta2.repeat(2 * b_size + 1, 1).view(2 * b_size,
                                                    2 * b_size + 1,
                                                    num_features)[1:b_size + 1,
                                                                  :b_size]
    v_prev = torch.cat((v_p.reshape(b_size, 1, num_features), beta2cir), 1)

    v_new = (weight_d * v_prev).sum(1)

    alpha_p = alpha
    beta_p = beta

    vp = torch.cat((v_p[-1].unsqueeze(0), v_new[:-1]), 0)

    return delta - vp.view(b_size, num_features, 1, 1) * (1 - abkw) * out, v_new, alpha_p, beta_p


class ControlNorm2D(nn.Module):
    r"""Applies Control Normalization (the per-channel exponential moving
    average, ema, forward and control process backward part of the Online
    Normalization algorithm) over a 4D input (a mini-batch of 3D inputs) as
    described in the paper:
    `Online Normalization for Training Neural Networks`.

    .. math::
        y_t = \frac{x_t - \mu_{t-1}}{\sqrt{\sigma^2_{t-1} + \epsilon}}

        \sigma^2_t = \alpha_fwd * \sigma^2_{t-1} + \alpha_fwd * (1 - \alpha_fwd) * (x_t - \mu_{t-1}) ^ 2
        \mu_t = \alpha_fwd * \mu_{t-1} + (1 - \alpha_fwd) * x_t

    The mean and standard-deviation are estimated per-channel.

    The math above represents the calculations occurring in the layer. To
    speed up computation with batched training we linearize the computation
    along the batch dimension and use convolutions in place of sums to
    distribute the computation across compute fabric.

    Args:
        num_features: :math:`L` from an expected input of size :math:`(N, L)`
        eps: a value added to the denominator for numerical stability.
            Default: 1e-5
        alpha_fwd: the decay factor to be used in fprop to update statistics.
            Default: 0.999
        alpha_bkw: the decay factor to be used in fprop to control the gradients
            propagating through the network. Default: 0.99
        b_size (N): in order to speed up computation we need to know and fix the
            batch size a priori.

    Shape:
        - Input: :math:`(N, C, H, W)`
        - Output: :math:`(N, C, H, W)` (same shape as input)

    Examples::

        >>> norm = ControlNorm2D(256, 0.999, 0.99)
        >>> input = torch.randn(32, 256, 32, 32)
        >>> output = norm(input)
    """
    __constants__ = ['m', 'var', 'u', 'v', 'm_p', 'var_p', 'u_p', 'v_p',
                     'beta_p', 'alpha_p', 'afwd', 'abkw', 'eps']

    def __init__(self, num_features, alpha_fwd=0.999, alpha_bkw=0.99,
                 eps=1e-05, b_size=None, **kwargs):
        super(ControlNorm2D, self).__init__()
        assert isinstance(b_size, int), 'b_size must be an integer'
        assert b_size > 0, 'b_size must be greater than 0'
        self.num_features = num_features
        self.eps = eps
        self.b_size = b_size

        self.afwd = alpha_fwd
        self.abkw = alpha_bkw

        # batch streaming parameters fpass
        self.af_pow = None
        self.af_batch = None

        # batch streaming parameters bpass
        self.ab_pow = None
        self.ab_batch = None

        # self.m and self.var are the streaming mean and variance respectively
        self.register_buffer('m', torch.zeros([b_size, num_features]))
        self.register_buffer('var', torch.ones([b_size, num_features]))
        self.register_buffer('m_p', torch.zeros([b_size, num_features]))
        self.register_buffer('var_p', torch.ones([b_size, num_features]))

        # self.u and self.v are the control variables respectively
        self.register_buffer('u', torch.zeros([b_size, num_features]))
        self.register_buffer('u_p', torch.zeros([b_size, num_features]))
        self.register_buffer('v_p', torch.zeros([b_size, num_features]))
        self.register_buffer('beta_p', torch.zeros([b_size, num_features]))
        self.register_buffer('alpha_p', torch.ones([b_size, num_features]))
        self.init_norm_params()

        class ControlNormalization(torch.autograd.Function):
            @staticmethod
            def forward(ctx, input):
                if self.af_pow is None:
                    range_b = torch.arange(self.b_size - 1, -1, -1).type(input.type())
                    self.af_pow = (self.afwd ** range_b).view(1, 1, -1)
                    self.af_batch = input.new_full((self.b_size, num_features),
                                                   self.afwd ** self.b_size)
                    self.ab_pow = (self.abkw ** range_b).view(1, 1, -1)
                    self.ab_batch = input.new_full((self.b_size, num_features),
                                                   self.abkw ** self.b_size)

                momentum = self.afwd
                momentum_pow = self.af_pow
                momentum_batch = self.af_batch

                mu, var = self.moments(input)

                _mu_b, mu_b = lin_momentum(self.m_p, mu, self.m, momentum,
                                           momentum_pow, momentum_batch)

                var_current = var + momentum * (mu - _mu_b) ** 2
                _var_b, var_b = lin_momentum(self.var_p, var_current, self.var,
                                             momentum, momentum_pow,
                                             momentum_batch)

                scale = torch.sqrt(_var_b + self.eps).unsqueeze(-1).unsqueeze(-1)
                out = (input - _mu_b.unsqueeze(-1).unsqueeze(-1)) / scale
                ctx.save_for_backward(out, scale)

                self.m_p.data = mu.clone()
                self.var_p.data = var_current.clone()

                self.m.data = mu_b.clone()
                self.var.data = var_b.clone()

                return out

            @staticmethod
            def backward(ctx, grad_in):
                out, scale, = ctx.saved_tensors

                # v controller
                lin_ctrl_out = lin_crtl(grad_in, out,
                                        self.b_size, self.num_features,
                                        self.v_p, self.alpha_p, self.beta_p,
                                        abkw=self.abkw, eps=1e-5)

                (grad_delta, self.v_p.data,
                 self.alpha_p.data, self.beta_p.data) = lin_ctrl_out

                grad_delta = grad_delta / scale

                # mean (u) controller
                u_tmp = self.mean(grad_delta)

                _u_b, u_b = lin_momentum(self.u_p, u_tmp, self.u, self.abkw,
                                         self.ab_pow, self.ab_batch)
                grad_delta = grad_delta - _u_b.unsqueeze(-1).unsqueeze(-1)
                self.u_p.data, self.u.data = u_tmp.clone(), u_b.clone()

                return grad_delta

        self.normalizer = ControlNormalization.apply

    def init_norm_params(self):
        nn.init.constant_(self.m, 0)
        nn.init.constant_(self.var, 1)
        nn.init.constant_(self.u, 0)
        nn.init.constant_(self.m_p, 0)
        nn.init.constant_(self.var_p, 1)
        nn.init.constant_(self.u_p, 0)
        nn.init.constant_(self.v_p, 0)

        nn.init.constant_(self.beta_p, 0)
        nn.init.constant_(self.alpha_p, 1)

    def moments(self, inputs):
        n = inputs.size(2) * inputs.size(3)

        mu = torch.sum(inputs, dim=(2, 3), keepdim=True) / n
        mu0 = inputs - mu
        return (mu.squeeze(),
                torch.sum(mu0 * mu0, dim=(2, 3), keepdim=False) / n)

    def mean(self, inputs):
        n = inputs.size(2) * inputs.size(3)
        return torch.sum(inputs, dim=(2, 3), keepdim=False) / n

    def extra_repr(self):
        s = (f'num_features={self.num_features}, afwd={self.afwd}, '
             f'abkw={self.abkw}, eps={self.eps}')
        return s

    def forward(self, input):
        if self.training:
            return self.normalizer(input)
        mu = self.m[-1].unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        var = self.var[-1].unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        stddev = torch.sqrt(var + self.eps)
        return (input - mu) / stddev


class OnlineNorm2D(nn.Module):
    r"""Applies Online Normalization over a 4D input (a mini-batch of 3D
    inputs) as described in the paper:
    `Online Normalization for Training Neural Networks`.

    .. math::
        y_t = LayerScaling(ControlNorm2D(x_t) * \gamma + \beta)

    Args:
        num_features: :math:`L` from an expected input of size :math:`(N, L)`
        eps: a value added to the denominator for numerical stability.
            Default: 1e-5
        alpha_fwd: the decay factor to be used in fprop to update statistics.
            Default: 0.999
        alpha_bkw: the decay factor to be used in fprop to control the gradients
            propagating through the network. Default: 0.99
        b_size: in order to speed up computation we need to know and fix the
            batch size a priori.
        weight: a boolean value that when set to ``True``, this module has
            learnable linear parameters. Default: ``True``
        bias: a boolean value that when set to ``True``, this module has
            learnable bias parameters. Default: ``True``
        ctrl_norm: control norm object layer. If None ControlNorm1D is selected.
            Use if you want to select ``ControlNorm1DLoop``
            Default: None
        layer_scaling: a boolean value that when set to ``True``, this module has
            layer scaling at the end. Default: ``True``

    Shape:
        - Input: :math:`(N, C, H, W)`
        - Output: :math:`(N, C, H, W)` (same shape as input)

    Examples::

        >>> # With Learnable Parameters
        >>> norm = OnlineNorm2D(128, 0.999, 0.99)
        >>> # Without Learnable Parameters
        >>> norm = OnlineNorm2D(128, 0.999, 0.99, weight=False, bias=False)
        >>> # With ControlNorm2DLoop
        >>> onloop = OnlineNorm2D(128,
                                  ctrl_norm=ControlNorm2DLoop(128, 0.999, 0.99))
        >>> input = torch.randn(64, 128, 32, 32)
        >>> output = norm(input)
    """
    __constants__ = ['weight', 'bias']

    def __init__(self, num_features, alpha_fwd=0.999, alpha_bkw=0.99,
                 eps=1e-05, weight=True, bias=True, ctrl_norm=None,
                 layer_scaling=True, **kwargs):
        super(OnlineNorm2D, self).__init__()
        self.num_features = num_features

        if ctrl_norm is not None:
            assert isinstance(ctrl_norm, (ControlNorm2DLoop,
                                      ControlNorm2D))
            self.ctrl_norm = ctrl_norm
        else:
            self.ctrl_norm = ControlNorm2D(num_features,
                                           alpha_fwd=alpha_fwd,
                                           alpha_bkw=alpha_bkw,
                                           eps=eps, **kwargs)

        if layer_scaling:
            self.layer_scaling = LayerScaling(eps=eps, **kwargs)
            warnings.warn('Using LS in Online Normalization')
        else:
            warnings.warn('Not using LS in Online Normalization')
        self.ls_op = layer_scaling

        if weight:
            self.weight = nn.Parameter(torch.ones([num_features]),
                                       requires_grad=True)
        else:
            self.register_parameter('weight', None)
        if bias:
            self.bias = nn.Parameter(torch.zeros([num_features]),
                                     requires_grad=True)
        else:
            self.register_parameter('bias', None)

    def extra_repr(self):
        return (f'num_features={self.num_features}, '
                f'weight={self.weight is not None}, '
                f'bias={self.bias is not None}')

    def forward(self, input):
        # apply control norm
        out = self.ctrl_norm(input)
        # scale output
        if self.weight is not None:
            out = out * self.weight.unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        # add bias
        if self.bias is not None:
            out = out + self.bias.unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
        # apply layer scaling
        return self.layer_scaling(out) if self.ls_op else out
