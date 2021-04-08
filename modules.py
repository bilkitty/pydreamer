import numpy as np
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


def init_weights_tf2(m):
    # Match TF2 initializations
    if type(m) in {nn.Conv2d, nn.ConvTranspose2d, nn.Linear}:
        nn.init.xavier_uniform_(m.weight.data)
        nn.init.zeros_(m.bias.data)
    if type(m) == nn.GRUCell:
        nn.init.xavier_uniform_(m.weight_ih.data)
        nn.init.orthogonal_(m.weight_hh.data)
        nn.init.zeros_(m.bias_ih.data)
        nn.init.zeros_(m.bias_hh.data)


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
        priors = []
        posts = []
        states = []
        state = in_state

        for i in range(n):
            prior, post, state = self._cell(embed[i], action[i], reset[i], state)
            priors.append(prior)
            posts.append(post)
            states.append(state)

        return (
            torch.stack(priors),          # tensor(N, B, 2*S)
            torch.stack(posts),           # tensor(N, B, 2*S)
            torch.stack(states),         # tensor(N, B, D+S)
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
            cat(h, sample),   # tensor(B, D+S)
        )


class ConvEncoder(nn.Module):

    def __init__(self, in_channels=3, kernels=(4, 4, 4, 4), stride=2, out_dim=256, activation=nn.ELU):
        super().__init__()
        self.out_dim = out_dim
        assert out_dim == 256
        self._model = nn.Sequential(
            nn.Conv2d(in_channels, 32, kernels[0], stride),
            activation(),
            nn.Conv2d(32, 64, kernels[1], stride),
            activation(),
            nn.Conv2d(64, 128, kernels[2], stride),
            activation(),
            nn.Conv2d(128, 256, kernels[3], stride),
            activation(),
            nn.Flatten()
        )

    def forward(self, x):
        return self._model(x)


class ConvDecoderCat(nn.Module):

    def __init__(self, in_dim, out_channels=3, kernels=(5, 5, 6, 6), stride=2, activation=nn.ELU):
        super().__init__()
        self.in_dim = in_dim
        self._model = nn.Sequential(
            # FC
            nn.Linear(in_dim, 1024),  # No activation here in DreamerV2
            nn.Unflatten(-1, (1024, 1, 1)),  # type: ignore
            # Deconv
            nn.ConvTranspose2d(1024, 128, kernels[0], stride),
            activation(),
            nn.ConvTranspose2d(128, 64, kernels[1], stride),
            activation(),
            nn.ConvTranspose2d(64, 32, kernels[2], stride),
            activation(),
            nn.ConvTranspose2d(32, out_channels, kernels[3], stride))

    def forward(self, x):
        return self._model(x)

    def loss(self, output, target):
        n = output.size(0)
        output = flatten(output)
        target = flatten(target).argmax(dim=-3)
        loss = F.cross_entropy(output, target, reduction='none')
        loss = unflatten(loss, n)
        return loss.sum(dim=[-1, -2])


class DenseEncoder(nn.Module):

    def __init__(self, in_dim, out_dim=256, activation=nn.ELU, hidden_dim=400, hidden_layers=2):
        super().__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        layers = [nn.Flatten()]
        layers += [
            nn.Linear(in_dim, hidden_dim),
            activation()]
        for _ in range(hidden_layers - 1):
            layers += [
                nn.Linear(hidden_dim, hidden_dim),
                activation()]
        layers += [
            nn.Linear(hidden_dim, out_dim),
            activation()]
        self._model = nn.Sequential(*layers)

    def forward(self, x):
        return self._model(x)


class DenseDecoder(nn.Module):

    def __init__(self, in_dim, out_shape=(33, 7, 7), activation=nn.ELU, hidden_dim=400, hidden_layers=2):
        super().__init__()
        self.in_dim = in_dim
        layers = []
        layers += [
            nn.Linear(in_dim, hidden_dim),
            activation()]
        for _ in range(hidden_layers - 1):
            layers += [
                nn.Linear(hidden_dim, hidden_dim),
                activation()]
        layers += [
            nn.Linear(hidden_dim, np.prod(out_shape)),
            nn.Unflatten(-1, out_shape)]
        self._model = nn.Sequential(*layers)

    def forward(self, x):
        return self._model(x)

    def loss(self,
             output,  # (N,B C,H,W)
             target   # float(N,B,C,H,W) or int(N,B,H,W)
             ):
        n = output.size(0)
        output = flatten(output)  # (N,B,C,H,W) => (NB,C,H,W)
        target = flatten(target)  # (N,B,...) => (NB,...)
        if output.shape == target.shape:
            target = target.argmax(dim=-3)  # float(NB,C,H,W) => int(NB,H,W)
        loss = F.cross_entropy(output, target, reduction='none')  # target: (NB,H,W)
        loss = unflatten(loss, n)
        return loss.sum(dim=[-1, -2])


class GlobalStateCore(nn.Module):

    def __init__(self, embed_dim=256, action_dim=7, mem_dim=200, stoch_dim=30, hidden_dim=200, min_std=0.1):
        super().__init__()
        self._cell = GlobalStateCell(embed_dim, action_dim, mem_dim, stoch_dim, hidden_dim, min_std)

    def forward(self,
                embed,     # tensor(N, B, E)
                action,    # tensor(N, B, A)
                reset,     # tensor(N, B)
                in_state_post,  # (tensor(B, M), tensor(B, S))
                ):

        n = embed.size(0)
        states = []
        posts = []
        state = in_state_post[0]

        for i in range(n):
            state, post = self._cell(embed[i], action[i], reset[i], state)
            states.append(state)
            posts.append(post)

        sample = diag_normal(posts[-1]).rsample()
        out_state_post = (states[-1].detach(), posts[-1].detach())

        return (
            sample,                      # tensor(   B, S)
            torch.stack(states),         # tensor(N, B, M)
            torch.stack(posts),          # tensor(N, B, 2S)
            in_state_post,               # (tensor(B, M), tensor(B, 2S)) - for loss
            out_state_post,              # (tensor(B, M), tensor(B, 2S))
        )

    def init_state(self, batch_size):
        return self._cell.init_state(batch_size)

    def loss(self,
             sample, states, posts, in_state_post, out_state_post,       # forward() output
             ):
        in_post = in_state_post[1]
        priors = torch.cat([in_post.unsqueeze(0), posts[:-1]])
        loss_kl = D.kl.kl_divergence(diag_normal(posts), diag_normal(priors))  # KL between consecutive posteriors
        loss_kl = loss_kl.mean()        # (N, B) => ()
        return loss_kl


class GlobalStateCell(nn.Module):

    def __init__(self, embed_dim=256, action_dim=7, mem_dim=200, stoch_dim=30, hidden_dim=200, min_std=0.1):
        super().__init__()
        self._mem_dim = mem_dim
        self._min_std = min_std

        self._ea_mlp = nn.Sequential(nn.Linear(embed_dim + action_dim, hidden_dim),
                                     nn.ELU())

        self._gru = nn.GRUCell(hidden_dim, mem_dim)

        self._post_mlp = nn.Sequential(nn.Linear(mem_dim, hidden_dim),
                                       nn.ELU(),
                                       nn.Linear(hidden_dim, 2 * stoch_dim))

    def init_state(self, batch_size):
        device = next(self._gru.parameters()).device
        state = torch.zeros((batch_size, self._mem_dim), device=device)
        post = self._post_mlp(state).detach()
        return (state, post)

    def forward(self,
                embed,     # tensor(B, E)
                action,    # tensor(B, A)
                reset,     # tensor(B)
                in_state,  # tensor(B, M)
                ):

        in_state = in_state * ~reset.unsqueeze(1)

        ea = self._ea_mlp(cat(embed, action))                                # (B, H)
        state = self._gru(ea, in_state)                                     # (B, M)
        post = to_mean_std(self._post_mlp(state), self._min_std)            # (B, 2*S)

        return (
            state,           # tensor(B, M)
            post,            # tensor(B, 2*S)
        )
