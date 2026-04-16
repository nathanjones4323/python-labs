# python-labs
Monorepo to hold my personal projects

## Secret Management (SOPS + age)

Secrets are stored encrypted in `.env.enc` at the repo root using [SOPS](https://github.com/getsops/sops) with [age](https://github.com/FiloSottile/age) encryption. The plaintext `.env` is git-ignored and never committed.

### Prerequisites

```bash
brew install sops age
```

### First-time setup

Generate your age key pair (one-time):

```bash
age-keygen -o ~/.config/sops/age/keys.txt
```

Share your **public key** with the repo owner to be added to `.sops.yaml`. Until then you cannot decrypt.

### Decrypt secrets to work locally

```bash
sops --decrypt --input-type dotenv --output-type dotenv .env.enc > .env
```

### Edit a secret

```bash
# Opens $EDITOR, re-encrypts automatically on save
sops .env.enc
```

### Add a new team member

1. Get their age public key (`age-keygen` outputs it)
2. Add it to `.sops.yaml` under `age:`, comma-separated
3. Re-encrypt: `sops --rotate --in-place .env.enc`
4. Commit `.sops.yaml` and `.env.enc`

### CI/CD (GitHub Actions)

Store the private key as a repository secret (`SOPS_AGE_KEY`), then decrypt before use:

```yaml
- name: Decrypt secrets
  env:
    SOPS_AGE_KEY: ${{ secrets.SOPS_AGE_KEY }}
  run: |
    mkdir -p ~/.config/sops/age
    echo "$SOPS_AGE_KEY" > ~/.config/sops/age/keys.txt
    sops --decrypt --input-type dotenv --output-type dotenv .env.enc > .env
```
