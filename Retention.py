import aka.nn as nn
import aka.numpy as np

def RetentionBlock(**kwargs):
    def __init__(self,**kwargs):
        args = nn.Object(**kwargs)
        self.embed_dim = args.latent_dim
        self.value_dim = getattr(args, 'hidden_dim', args.latent_dim)
        self.num_heads = args.num_heads
        self.head_dim = self.value_dim // self.num_heads
        self.key_dim = self.embed_dim // self.num_heads
        self.scaling = self.key_dim**-0.5
        self.gate_fn = np.silu

        self.q_proj = nn.Linear(self.embed_dim, self.embed_dim, bias=args.bias)
        self.k_proj = nn.Linear(self.embed_dim, self.embed_dim, bias=args.bias)
        self.v_proj = nn.Linear(self.embed_dim, self.value_dim, bias=args.bias)
        self.g_proj = nn.Linear(self.embed_dim, self.value_dim, bias=args.bias)
        self.out_proj = nn.Linear(self.value_dim, self.embed_dim, bias=args.bias)
        self.group_norm = nn.RMSNorm(self.head_dim)
        self.decay = nn.Parameter(
            data=np.log(1 - 2 ** (-5 - np.arange(self.num_heads, dtype=np.float))),
            requires_grad=getattr(args,'lr',False)
        )
        return self

    def compute_mask(decay, qlen, klen, num_heads):
        if qlen == 1:
            return np.exp(decay)[None, :, None, None]
        else:
            index = np.arange(klen).to(decay)
            mask = np.tril(np.ones(qlen, klen), diagonal=klen-qlen).to(decay)
            mask = np.masked_fill(
                index[-qlen:, None] - index[None, :], ~mask.bool(), float("inf")
            )
            mask = np.exp(mask * decay[:, None, None])
            mask = np.nan_to_num(mask)
            mask = mask.unsqueeze(0)  # [1, h, t, t]
            mask = mask / mask.sum(dim=-1, keepdim=True).sqrt()
            mask = np.nan_to_num(mask, nan=0.0)
            return mask

    def apply_rotary_emb(x, cache, pos=0):
        _,_,L,D = x.shape
        slen = pos+L
        emb = cache.get('rotary_emb', None)
        if emb is None or len(emb[0]) < slen:
            angle = 1.0 / (10000 ** np.linspace(0, 1, D//2))
            angle = angle.unsqueeze(-1).repeat(1, 2).flatten()
            index = np.arange(slen)
            sin = np.sin(index[:, None] * angle[None, :])
            cos = np.cos(index[:, None] * angle[None, :])
            cache['rotary_emb'] = (sin, cos)
        else:
            (sin,cos) = emb
        x1 = x[:, :, :, ::2]
        x2 = x[:, :, :, 1::2]
        y = np.stack((-x2, x1), dim=-1).flatten(-2)
        return (x * cos[pos:pos+L]) + (y * sin[pos:pos+L])

    def forward(self, x, cache={}, state=None, **kwargs): 
        B, T, H = x.size()
        q, k, v, g = [proj(x) for proj in [self.q_proj, self.k_proj, self.v_proj, self.g_proj]]
        q, k, v = [t.view(B, T, self.num_heads, -1) for t in [q,k,v]]
        k *= self.scaling

        # -- rotary embedding --
        q, k, v = [np.rearrange('b l n d -> b n l d', t) for t in [q, k, v]]
        q = apply_rotary_emb(q, cache)
        k = apply_rotary_emb(k, cache)

        # -- qkv (Q @ K * D) @ V --
        decay_mask = compute_mask(self.decay, q.size(2), k.size(2), self.num_heads)
        y = np.einsum('bhld,bhmd,bhlm,bhmv->blhv', q, k, decay_mask, v)

        # -- state --
        if state is not None:
            current_S = np.einsum('bhld,bhlv,bhl->bhvd', k, v, decay_mask[:, :, -1])
            prev_S = state.get('prev_S',None) # ->[b, h, d, v]
            if prev_S is not None:
                decay = decay_mask[:, :, :, 0] # ->[b, h, t]
                # S += S0 * (gamma ** n)
                current_S += np.einsum('bhvd,bh->bhvd', prev_S, decay[:,:,-1])
                # V += Q @ decay * S0
                y += np.einsum('bhld,bhvd,bhl->blhv', q, prev_S, decay)
            state["prev_S"] = current_S.detach()

        # norm
        normed = self.group_norm(y).reshape(B, x.size(1), self.value_dim)
        out = self.gate_fn(g) * normed
        return self.out_proj(out)
    return __init__(nn.Module(forward=forward), **kwargs)

def RetentionArgs(name):
    args = nn.Object(
        vocab_dim = 32,
        latent_dim = 384,
        dropout = 0.1,
        bias = False, # bias in Linear?
    )
    mlp_args = nn.Object(
        name = 'MLP',
        k_dim = 64,
        k_size = 384 * 3,
        kv_gate = False,
    ),
    attn_args = nn.Object(
        name = 'Retention',
        num_heads = 8,
        num_kv_groups = 8,
        rotary_embedding = True
    ),
    match name:
        case 'Ret':
            args.layers = [attn_args, mlp_args]*3
        case _:
            assert False, f"Unknown Ret name{name}"
    return args

def RetNet(name):
    import aka.repo as repo

    # -- Tokenizer --
    tokenizer = repo.AutoTokenizer(name)
    cfg = repo.fopen(name, 'config.json', ftype='json')
    args = nn.Object(
        tokenizer = tokenizer,
        vocab_size = cfg['vocab_size'],
        embedding_scale = True,
        latent_dim = cfg['decoder_embed_dim'],
        lm_head = True,
        prev_norm = 'rms',
        bias = False,
        dropout = cfg['dropout'],
        layers = [
            nn.Object(
                name = 'Retention',
                num_heads = cfg['decoder_retention_heads'],
                hidden_dim = cfg['decoder_value_embed_dim'],
            ), 
            nn.Object(
                name = 'MLP',
                k_size = cfg['decoder_ffn_embed_dim'],
                kv_gate = cfg['use_glu'],
                activation = cfg['activation_fn']
            )
        ]*cfg['decoder_layers']
    )

    # -- Model --
    from CausalLM import CausalLM
    m = CausalLM(**args)
    if repo.exist(name, "model.safetensors"):
        with repo.fopen(name, "model.safetensors", ftype='safetensor') as f:
            keys = f.keys()
            with np.no_grad():
                m.lm_head.weight.copy_(f.get_tensor(f'lm_head.weight'))
                m.embedding.weight.copy_(f.get_tensor('model.embed_tokens.weight'))
                m.post_norm.weight.copy_(f.get_tensor(f'model.layer_norm.weight'))
                m.prev_norm.weight.copy_(f.get_tensor(f'model.layernorm_embedding.weight'))
                for i in range(len(m.layers)//2):
                    m.layers[i*2].norm.weight.copy_(f.get_tensor(f'model.layers.{i}.retention_layer_norm.weight'))
                    m.layers[i*2].layer.q_proj.weight.copy_(f.get_tensor(f'model.layers.{i}.retention.q_proj.weight'))
                    m.layers[i*2].layer.g_proj.weight.copy_(f.get_tensor(f'model.layers.{i}.retention.g_proj.weight'))
                    m.layers[i*2].layer.k_proj.weight.copy_(f.get_tensor(f'model.layers.{i}.retention.k_proj.weight'))
                    m.layers[i*2].layer.v_proj.weight.copy_(f.get_tensor(f'model.layers.{i}.retention.v_proj.weight'))
                    m.layers[i*2].layer.out_proj.weight.copy_(f.get_tensor(f'model.layers.{i}.retention.out_proj.weight'))
                    m.layers[i*2+1].norm.weight.copy_(f.get_tensor(f'model.layers.{i}.final_layer_norm.weight'))

                    # Take care here. gate and fc1 are just swaped.
                    m.layers[i*2+1].layer.gate_proj.data.copy_(f.get_tensor(f'model.layers.{i}.ffn.fc1.weight'))
                    m.layers[i*2+1].layer.up_proj.data.copy_(f.get_tensor(f'model.layers.{i}.ffn.gate.weight'))
                    m.layers[i*2+1].layer.down_proj.data.copy_(f.get_tensor(f'model.layers.{i}.ffn.fc2.weight'))
    return m

if __name__ == "__main__":
    m = RetNet('data/SDPrompt-RetNet-300M')
    print('Model loaded')
    for w in m.generator("1girl"):
        print(w, end='')
# <s> 1girl, absurdres, animal ear fluff, animal ears, bangs, bare shoulders, black hair, blue archive, blunt bangs, blush, closed mouth, collarbone, commentary request, eyes visible through hair, green eyes, hair between eyes, halo, hand on own face, hand up, highres, jacket, kisaki blue archive, long hair, long sleeves, looking at viewer, open clothes, open jacket, shinonome asu, simple background, solo, track jacket, upper body, white background, white jacket</s>
    # from RomeArena import TrainRoles
    # TrainRoles([
    #     # 'Gemma-20m', 
    #     'Retention-Ret',
    # ], lr = 6e-4, epochs=4)
