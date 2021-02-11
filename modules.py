import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as D


def flatten(x):
    # (N, B, ...) => (N*B, ...)
    return torch.reshape(x, (-1,) + x.shape[2:])


def unflatten(x, n):
    # (N*B, ...) => (N, B, ...)
    return torch.reshape(x, (n, -1) + x.shape[1:])


def cat(x1, x2):
    # (..., A), (..., B) => (..., A+B)
    return torch.cat((x1, x2), dim=-1)


def split(mean_std, sizes=None):
    # (..., S+S) => (..., S), (..., S)
    if sizes == None:
        sizes = mean_std.size(-1) // 2
    mean, std = mean_std.split(sizes, dim=-1)
    return mean, std


def diag_normal(mean_std):
    mean, std = split(mean_std)
    return D.independent.Independent(D.normal.Normal(mean, std), 1)


def to_mean_std(x, min_std):
    mean, std = split(x)
    std = F.softplus(std) + min_std
    return cat(mean, std)


def zero_prior_like(mean_std):
    # Returns prior with 0 mean and unit variance
    mean, std = split(mean_std)
    prior = cat(torch.zeros_like(mean), torch.ones_like(std))
    return prior


class RSSMCore(nn.Module):

    def __init__(self, embed_dim=256, action_dim=7, deter_dim=200, stoch_dim=30, hidden_dim=200, min_std=0.1):
        super().__init__()
        self._cell = RSSMCell(embed_dim, action_dim, deter_dim, stoch_dim, hidden_dim, min_std)

    def forward(self,
                embed,     # tensor(N, B, E)
                action,    # tensor(N, B, A)
                reset,     # tensor(N, B)
                in_state,  # tensor(   B, D+S)
                ):

        n = embed.size(0)
        prior = []
        post = []
        post_sample = []
        state = in_state

        for i in range(n):
            prior_i, post_i, sample_i, state = self._cell(embed[i], action[i], reset[i], state)
            prior.append(prior_i)
            post.append(post_i)
            post_sample.append(sample_i)

        return (
            torch.stack(prior),          # tensor(N, B, 2*S)
            torch.stack(post),           # tensor(N, B, 2*S)
            torch.stack(post_sample),    # tensor(N, B, S)
            state,                       # tensor(   B, D+S)
        )

    def init_state(self, batch_size):
        return self._cell.init_state(batch_size)


class RSSMCell(nn.Module):

    def __init__(self, embed_dim=256, action_dim=7, deter_dim=200, stoch_dim=30, hidden_dim=200, min_std=0.1):
        super().__init__()
        self._stoch_dim = stoch_dim
        self._deter_dim = deter_dim
        self._min_std = min_std

        self._za_mlp = nn.Sequential(nn.Linear(stoch_dim + action_dim, hidden_dim),
                                     nn.ELU())

        self._gru = nn.GRUCell(hidden_dim, deter_dim)

        self._prior_mlp = nn.Sequential(nn.Linear(deter_dim, hidden_dim),
                                        nn.ELU(),
                                        nn.Linear(hidden_dim, 2 * stoch_dim))

        self._post_mlp = nn.Sequential(nn.Linear(deter_dim + embed_dim, hidden_dim),
                                       nn.ELU(),
                                       nn.Linear(hidden_dim, 2 * stoch_dim))

    def init_state(self, batch_size):
        device = next(self._gru.parameters()).device
        return torch.zeros((batch_size, self._deter_dim + self._stoch_dim), device=device)

    def forward(self,
                embed,     # tensor(B, E)
                action,    # tensor(B, A)
                reset,     # tensor(B)
                in_state,  # tensor(B, D+S)
                ):

        in_state = in_state * ~reset.unsqueeze(1)
        in_h, in_z = split(in_state, [self._deter_dim, self._stoch_dim])

        za = self._za_mlp(cat(in_z, action))                                # (B, H)
        h = self._gru(za, in_h)                                             # (B, D)
        prior = to_mean_std(self._prior_mlp(h), self._min_std)              # (B, 2*S)
        post = to_mean_std(self._post_mlp(cat(h, embed)), self._min_std)    # (B, 2*S)
        sample = diag_normal(post).rsample()                                # (B, S)

        return (
            prior,            # tensor(B, 2*S)
            post,             # tensor(B, 2*S)
            sample,           # tensor(B, S)
            cat(h, sample),   # tensor(B, D+S)
        )


class ConvEncoder(nn.Module):

    def __init__(self, in_channels=3, kernels=(4, 4, 4, 4), stride=2, out_dim=256):
        super().__init__()
        self._model = nn.Sequential(
            nn.Conv2d(in_channels, 32, kernels[0], stride),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernels[1], stride),
            nn.ReLU(),
            nn.Conv2d(64, 128, kernels[2], stride),
            nn.ReLU(),
            nn.Conv2d(128, out_dim, kernels[3], stride),
            nn.ReLU(),
            nn.Flatten()
            )
        self.out_dim = out_dim

    def forward(self, x):
        return self._model(x)


class MinigridDecoderCE(nn.Module):

    def __init__(self, in_dim=30):
        super().__init__()
        self._model = nn.Sequential(
            nn.Linear(in_dim, 256),
            nn.ELU(),
            nn.Linear(256, 256),
            nn.ELU(),
            nn.Linear(256, 33 * 7 * 7),
            nn.Unflatten(-1, (33, 7, 7)),
        )
        self.in_dim = in_dim

    def forward(self, x):
        return self._model(x)

    def loss(self, output, target):
        n = output.size(0)
        output = flatten(output)
        target = flatten(target).argmax(dim=-3)
        loss = F.cross_entropy(output, target, reduction='none')
        loss = unflatten(loss, n)
        return loss.sum(dim=[-1, -2])
