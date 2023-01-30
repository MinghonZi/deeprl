"""
TODO 3.9
Generic Alias Type and PEP 585.

TODO 3.10
Adopt PEP 604.

TODO 3.10
Convert multiple isinstance checks to structural pattern matching (PEP 634).

TODO
Proper type hint for functools.partial.

FIXME
Hard-code action range

FIXME
Annotate function approximators as nn.Module is unsafe because the type annotations of
forward method are not checked.
"""

from copy import deepcopy
from functools import partial
from itertools import chain, cycle
from typing import Callable, Iterator, Optional, Protocol, runtime_checkable

import torch
import torch.nn as nn
import torch.nn.functional as F
from cytoolz import comp
from cytoolz.curried import map, reduce
from torch import Tensor, add, min
from torch.nn.parameter import Parameter
from torch.optim import Optimizer

from .experience_replay import Batch


class ExperienceReplay(Protocol):
    def push(
        self,
        state: Tensor,
        action: Tensor,
        reward: Tensor,
        next_state: Tensor,
        terminated: Tensor,
    ) -> None:
        ...

    def sample(self, batch_size: int) -> Batch:
        ...


@runtime_checkable
class ActionNoise(Protocol):
    def __call__(self, action: Tensor) -> Tensor:
        ...


class TD3:
    """Twin-Delayed DDPG"""

    def __init__(
        self,
        policy: nn.Module,
        quality: nn.Module,
        policy_optimiser_init: Callable[[Iterator[Parameter]], Optimizer],
        quality_optimiser_init: Callable[[Iterator[Parameter]], Optimizer],
        experience_replay: ExperienceReplay,
        batch_size: int,
        discount_factor: float,
        polyak_factor: float,
        exploration_noise: Optional[ActionNoise],
        smoothing_noise_stdev: float,
        smoothing_noise_clip: float,  # Norm length to clip target policy smoothing noise
        num_qualities: int = 2,
        policy_delay: int = 2,
        device: Optional[torch.device] = None,
    ) -> None:

        self._policy = policy.to(device)
        self._qualities = [deepcopy(quality.to(device)) for _ in range(num_qualities)]
        self._target_policy = deepcopy(self._policy)
        self._target_qualities = deepcopy(self._qualities)
        # Freeze target networks with respect to optimisers (only update via Polyak averaging)
        self._target_policy.requires_grad_(False)
        [net.requires_grad_(False) for net in self._target_qualities]

        self._policy_optimiser = policy_optimiser_init(self._policy.parameters())
        self._quality_optimiser = quality_optimiser_init(
            chain(*[quality.parameters() for quality in self._qualities])
        )

        self._experience_replay = experience_replay
        self._batch_size = batch_size

        self._discount_factor = discount_factor
        self._polyak_factor = polyak_factor
        self._exploration_noise = exploration_noise
        self._smoothing_noise_clip = smoothing_noise_clip
        self._smoothing_noise_stdev = smoothing_noise_stdev
        self._policy_delay = cycle(range(policy_delay))

        self.device = device

    def step(
        self,
        state: Tensor,
        action: Tensor,
        reward: Tensor,
        next_state: Tensor,
        terminated: Tensor,
    ) -> None:
        self._experience_replay.push(state, action, reward, next_state, terminated)
        self._update_parameters()

    def _update_parameters(self) -> None:

        try:
            batch = self._experience_replay.sample(self._batch_size)
        except ValueError:
            return

        # Abbreviating to mathematical italic unicode char for readability
        𝑠 = batch.states
        𝘢 = batch.actions
        𝑟 = batch.rewards
        𝑠ʼ = batch.next_states
        𝑑 = batch.terminateds
        𝛾 = self._discount_factor
        𝜎 = self._smoothing_noise_stdev
        𝑐 = self._smoothing_noise_clip
        𝜇 = self._policy  # Deterministic policy is usually denoted by 𝜇
        𝜇ʼ = self._target_policy
        𝑄_ = self._qualities
        𝑄ʼ_ = self._target_qualities
        𝜏 = self._polyak_factor

        # Target policy smoothing: add clipped noise to the target action
        ϵ = (torch.randn_like(𝘢) * 𝜎).clamp(-𝑐, 𝑐)  # Clipped noise
        ã = (𝜇ʼ(𝑠ʼ) + ϵ).clamp(-1, 1)  # clipped to lie in valid action range

        # Clipped double-Q learning
        𝑦 = 𝑟 + ~𝑑 * 𝛾 * min(*[𝑄ʼ(𝑠ʼ, ã) for 𝑄ʼ in 𝑄ʼ_])  # computes learning target
        action_quality = [𝑄(𝑠, 𝘢) for 𝑄 in 𝑄_]
        quality_loss_fn = comp(reduce(add), map(partial(F.mse_loss, target=𝑦)))
        quality_loss: Tensor = quality_loss_fn(action_quality)
        self._quality_optimiser.zero_grad()
        quality_loss.backward()
        self._quality_optimiser.step()

        # "Delayed" policy updates
        if next(self._policy_delay) == 0:

            # Improve the deterministic policy just by maximizing the first quality fn approximator by gradient ascent
            policy_loss: Tensor = -𝑄_[0](𝑠, 𝜇(𝑠)).mean()
            self._policy_optimiser.zero_grad()
            policy_loss.backward()
            self._policy_optimiser.step()

            # Update frozen target fn approximators by Polyak averaging (exponential smoothing)
            with torch.no_grad():  # stops target param from requesting grad after calc because original param require grad are involved in the calc
                for 𝑄, 𝑄ʼ in zip(𝑄_, 𝑄ʼ_):
                    for 𝜃, 𝜃ʼ in zip(𝑄.parameters(), 𝑄ʼ.parameters()):
                        𝜃ʼ.copy_(𝜏 * 𝜃 + (1.0 - 𝜏) * 𝜃ʼ)
                for 𝜙, 𝜙ʼ in zip(𝜇.parameters(), 𝜇ʼ.parameters()):
                    𝜙ʼ.copy_(𝜏 * 𝜙 + (1.0 - 𝜏) * 𝜙ʼ)

    @torch.no_grad()
    def compute_action(self, state: Tensor) -> Tensor:
        action: Tensor = self._policy(state)
        if isinstance(self._exploration_noise, ActionNoise):
            noise = self._exploration_noise(action)
            action = (action + noise).clamp(-1, 1)
        return action
