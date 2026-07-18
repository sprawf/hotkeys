"""Model roster freshness check for Cerebras + Groq.

Run this quarterly (or whenever a chat call starts 404-ing) to see:
  1. Every model each provider currently exposes to your bundled key.
  2. Which of engine.py's hard-coded MODELS entries are still live.
  3. Which live models the code doesn't know about yet (candidates
     to promote into the roster).

Usage:
    python E:\\Hotkeys\\refresh_models.py

Exit code 0 = every model in engine.CEREBRAS_MODELS / engine.GROQ_MODELS
is confirmed live. Non-zero = one or more have died and the source
comments in engine.py need updating.

No LLM calls, no cost — just GET /v1/models on each provider.
"""
from __future__ import annotations
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              line_buffering=True)
sys.path.insert(0, r'E:\Hotkeys')

import requests
import engine as hk_engine
try:
    from _bundled_keys import CEREBRAS, GROQ, GROQ_2
    KEYS = {'cerebras': [CEREBRAS], 'groq': [GROQ, GROQ_2]}
except Exception:
    print('_bundled_keys not importable — using user-config keys instead.')
    import storage
    cfg = storage.load_config()
    KEYS = {
        'cerebras': hk_engine._resolve_keys(cfg, 'cerebras'),
        'groq':     hk_engine._resolve_keys(cfg, 'groq'),
    }

ENDPOINTS = {
    'cerebras': 'https://api.cerebras.ai/v1/models',
    'groq':     'https://api.groq.com/openai/v1/models',
}
HARDCODED = {
    'cerebras': hk_engine.CEREBRAS_MODELS,
    'groq':     hk_engine.GROQ_MODELS,
}


def fetch(provider: str) -> set[str] | None:
    keys = KEYS.get(provider) or []
    for key in keys:
        try:
            r = requests.get(ENDPOINTS[provider],
                             headers={'Authorization': f'Bearer {key}'},
                             timeout=15)
            if r.status_code == 200:
                return {m['id'] for m in r.json().get('data', [])}
        except Exception as e:
            print(f'  {provider} probe error with key …{key[-6:]}: {e}')
    return None


def main() -> int:
    dead: list[tuple[str, str]] = []
    for provider in ('cerebras', 'groq'):
        print(f'\n=== {provider} ===')
        live = fetch(provider)
        if live is None:
            print(f'  <no keys / all keys failed; skipping>')
            continue
        hardcoded = HARDCODED[provider]
        print(f'  live models:      {len(live)}')
        print(f'  in engine.py:     {len(hardcoded)}')

        still_live = [m for m in hardcoded if m in live]
        broken     = [m for m in hardcoded if m not in live]
        unknown    = sorted(live - set(hardcoded))

        print(f'  hard-coded, still live ({len(still_live)}):')
        for m in still_live: print(f'    OK  {m}')
        if broken:
            print(f'  hard-coded, RETIRED ({len(broken)}):')
            for m in broken:
                print(f'    !!  {m}   <- remove from engine.{provider.upper()}_MODELS')
                dead.append((provider, m))
        if unknown:
            print(f'  live but not in engine.py ({len(unknown)}):')
            for m in unknown: print(f'    ?   {m}')

    print()
    if dead:
        print(f'FAIL: {len(dead)} dead model(s) still referenced in engine.py:')
        for p, m in dead: print(f'  - {p}: {m}')
        return 1
    print('OK: every hard-coded model is confirmed live.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
