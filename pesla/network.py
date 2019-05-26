# -*- coding: utf-8 -*-
"""MLP module for model of algorithms

- Author: Kh Kim
- Contact: kh.kim@medipixel.io
"""

from typing import Callable, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal


def identity(x: torch.Tensor) -> torch.Tensor:
    """Return input without any change."""
    return x


def init_layer_xavier(layer: nn.Linear) -> nn.Linear:
    nn.init.xavier_uniform_(layer.weight)
    nn.init.zeros_(layer.bias)
    return layer


class MLP(nn.Module):
    """Baseline of Multilayer perceptron with LSTM output.

    Attributes:
        input_size (int): size of input
        output_size (int): size of output layer
        hidden_sizes (list): sizes of hidden layers
        hidden_activation (function): activation function of hidden layers
        output_activation (function): activation function of output layer
        hidden_layers (list): list containing linear layers
        use_output_layer (bool): whether or not to use the last layer
        n_category (int): category number (-1 if the action is continuous)
        use_lstm: bool = False

    """

    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_sizes: list,
        hidden_activation: Callable = F.relu,
        output_activation: Callable = identity,
        linear_layer: nn.Module = nn.Linear,
        use_output_layer: bool = True,
        n_category: int = -1,
        lstm_layer_size = 1,
        init_fn: Callable = init_layer_xavier
    ):
        """Initialization.

        Args:
            input_size (int): size of input
            output_size (int): size of output layer
            hidden_sizes (list): number of hidden layers
            hidden_activation (function): activation function of hidden layers
            output_activation (function): activation function of output layer
            linear_layer (nn.Module): linear layer of mlp
            use_output_layer (bool): whether or not to use the last layer
            n_category (int): category number (-1 if the action is continuous)
            init_fn (Callable): weight initialization function bound for the last layer

        """
        super(MLP, self).__init__()

        self.hidden_sizes = hidden_sizes
        self.input_size = input_size
        self.output_size = output_size
        self.hidden_activation = hidden_activation
        self.output_activation = output_activation
        self.linear_layer = linear_layer
        self.use_output_layer = use_output_layer
        self.n_category = n_category

        # set hidden layers
        self.hidden_layers: list = []
        in_size = self.input_size
        for i, next_size in enumerate(hidden_sizes):
            fc = self.linear_layer(in_size, next_size)
            in_size = next_size
            self.__setattr__("hidden_fc{}".format(i), fc)
            self.hidden_layers.append(fc)

        self.lstm_layer_size = lstm_layer_size
        self.lstm_size = in_size
        self.lstm_layer = nn.LSTM(in_size, in_size, self.lstm_layer_size)

        # set output layers
        if self.use_output_layer:
            self.output_layer = self.linear_layer(in_size, output_size)
            self.output_layer = init_fn(self.output_layer)
        else:
            self.output_layer = identity
            self.output_activation = identity

    def init_lstm_states(self, batch_size, device):
        hx = torch.zeros(self.lstm_layer_size, batch_size, self.lstm_size).float().to(device)
        cx = torch.zeros(self.lstm_layer_size, batch_size, self.lstm_size).float().to(device)

        return hx, cx

    def forward(self, x: torch.Tensor, batch_size, step_size, hx, cx) -> torch.Tensor:
        """Forward method implementation."""
        for hidden_layer in self.hidden_layers:
            x = self.hidden_activation(hidden_layer(x))

        x = x.view(step_size, batch_size, self.lstm_size)
        x, (hx, cx) = self.lstm_layer(x, (hx, cx))

        x = x.view(batch_size, step_size, -1)

        x = self.output_activation(self.output_layer(x))

        return x, hx, cx


class GaussianDist(MLP):
    """Multilayer perceptron with Gaussian distribution output.

    Attributes:
        mu_activation (function): bounding function for mean
        log_std_min (float): lower bound of log std
        log_std_max (float): upper bound of log std
        mu_layer (nn.Linear): output layer for mean
        log_std_layer (nn.Linear): output layer for log std
    """

    def __init__(
        self,
        input_size: int,
        output_size: int,
        hidden_sizes: list,
        hidden_activation: Callable = F.relu,
        mu_activation: Callable = torch.tanh,
        log_std_min: float = -20,
        log_std_max: float = 2,
        lstm_layer_size = 1,
        init_fn: Callable = init_layer_xavier
    ):
        """Initialization."""
        super(GaussianDist, self).__init__(
            input_size=input_size,
            output_size=output_size,
            hidden_sizes=hidden_sizes,
            hidden_activation=hidden_activation,
            lstm_layer_size=lstm_layer_size,
            use_output_layer=False
        )

        self.mu_activation = mu_activation
        self.log_std_min = log_std_min
        self.log_std_max = log_std_max
        in_size = hidden_sizes[-1]

        # set log_std layer
        self.log_std_layer = nn.Linear(in_size, output_size)
        self.log_std_layer = init_fn(self.log_std_layer)

        # set mean layer
        self.mu_layer = nn.Linear(in_size, output_size)
        self.mu_layer = init_fn(self.mu_layer)

    def get_dist_params(self, x: torch.Tensor, batch_size, step_size, hx, cx) -> Tuple[torch.Tensor, ...]:
        """Return gausian distribution parameters."""
        hidden, hx, cx = super(GaussianDist, self).forward(x, batch_size, step_size, hx, cx)

        # get mean
        mu = self.mu_activation(self.mu_layer(hidden))

        # get std
        log_std = torch.tanh(self.log_std_layer(hidden))
        log_std = self.log_std_min + 0.5 * (self.log_std_max - self.log_std_min) * (
            log_std + 1
        )
        std = torch.exp(log_std)

        return mu, log_std, std, hx, cx

    def forward(self, x: torch.Tensor, batch_size, step_size, hx, cx) -> Tuple[torch.Tensor, ...]:
        """Forward method implementation."""
        mu, _, std, hx, cx = self.get_dist_params(x, batch_size, step_size, hx, cx)

        # get normal distribution and action
        dist = Normal(mu, std)
        action = dist.sample()

        return action, dist, hx, cx


class TanhGaussianDistParams(GaussianDist):
    """Multilayer perceptron with Gaussian distribution output."""

    def __init__(self, **kwargs):
        """Initialization."""
        super(TanhGaussianDistParams, self).__init__(**kwargs, mu_activation=identity)

    def forward(self, x: torch.Tensor, batch_size, step_size, hx, cx, epsilon: float = 1e-6) -> Tuple[torch.Tensor, ...]:
        """Forward method implementation."""
        mu, _, std, hx, cx = super(TanhGaussianDistParams, self).get_dist_params(x, batch_size, step_size, hx, cx)

        # sampling actions
        dist = Normal(mu, std)
        z = dist.rsample()

        # normalize action and log_prob
        # see appendix C of 'https://arxiv.org/pdf/1812.05905.pdf'
        action = torch.tanh(z)
        log_prob = dist.log_prob(z) - torch.log(1 - action.pow(2) + epsilon)
        log_prob = log_prob.sum(-1, keepdim=True)

        return action, log_prob, z, mu, std, hx, cx

