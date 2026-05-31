"""
diarize_worker.py — standalone Hotkeys speaker-diarization worker.

Runs in its own process so torch + pyannote can load without conflicting
with the main Hotkeys process's ctranslate2 / onnxruntime / numpy / av
runtimes (those conflicts caused STATUS_STACK_BUFFER_OVERRUN 0xc0000409
heap corruption in the dist when everything was in one bundle).

USAGE
    diarize.exe <work_dir>

INPUT FILES (parent process writes these before spawning the worker)
    <work_dir>/input.npy   — float32 mono numpy waveform at 16 kHz
    <work_dir>/input.json  — {
        "sample_rate":  16000,
        "min_speakers": int | null,
        "max_speakers": int | null,
        "model_dir":    "<abs path to bundled diarization model>"
      }

OUTPUT FILE (worker writes this; parent reads it after worker exits)
    <work_dir>/output.json — on success:
        {
          "ok": true,
          "turns": [[start, end, "SPEAKER_00"], ...]
        }
      on failure:
        {
          "ok": false,
          "error": "<short message>",
          "tb":    "<full traceback>"
        }

EXIT CODES
    0  always (errors are reported via output.json; nonzero exit would
       only happen on truly catastrophic worker crashes, in which case
       output.json is absent and parent falls back gracefully)
"""
import os
import sys
import json
import traceback


def _write_response(work_dir: str, response: dict) -> None:
    out_path = os.path.join(work_dir, 'output.json')
    # Atomic-ish write: write to .tmp then rename.
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(response, f)
    try:
        os.replace(tmp, out_path)
    except Exception:
        # Last-ditch: at least leave the .tmp file so parent has *something*
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: diarize.exe <work_dir>', file=sys.stderr)
        return 2

    work_dir = sys.argv[1]
    if not os.path.isdir(work_dir):
        print(f'work_dir does not exist: {work_dir}', file=sys.stderr)
        return 2

    # Heavy imports happen INSIDE main() so an environment-level failure
    # (e.g. missing DLL) still produces a usable output.json instead of
    # the worker dying before it can write anything.
    try:
        with open(os.path.join(work_dir, 'input.json'), 'r', encoding='utf-8') as f:
            cfg = json.load(f)

        # Threading-runtime guards. The worker process bundles its own torch,
        # but if the user's system has Intel OpenMP elsewhere, the duplicate
        # tolerance flag prevents init-time crashes.
        os.environ.setdefault('KMP_DUPLICATE_LIB_OK', 'TRUE')
        os.environ.setdefault('OMP_NUM_THREADS',      '1')
        os.environ.setdefault('MKL_NUM_THREADS',      '1')
        os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

        import numpy as np
        waveform = np.load(os.path.join(work_dir, 'input.npy'))

        import torch
        # pyannote / torchaudio are noisy about torchcodec / lightning;
        # we don't care, swallow at import time.
        import warnings
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore')
            from pyannote.audio import Pipeline

        model_dir = cfg.get('model_dir') or ''
        if not model_dir or not os.path.isdir(model_dir):
            _write_response(work_dir, {
                'ok': False,
                'error': f'pyannote model directory not found: {model_dir!r}',
                'tb': '',
            })
            return 0

        pipeline = Pipeline.from_pretrained(model_dir)

        audio_in = {
            'waveform':    torch.from_numpy(np.ascontiguousarray(waveform)).unsqueeze(0),
            'sample_rate': int(cfg.get('sample_rate', 16000)),
        }

        kw: dict = {}
        if cfg.get('min_speakers') is not None:
            kw['min_speakers'] = int(cfg['min_speakers'])
        if cfg.get('max_speakers') is not None:
            kw['max_speakers'] = int(cfg['max_speakers'])

        with warnings.catch_warnings():
            warnings.filterwarnings('ignore')
            result = pipeline(audio_in, **kw)

        # pyannote community-1 returns a DiarizeOutput object with two
        # variants; older pyannote 2.x returns an Annotation directly.
        # The exclusive variant gives one speaker per moment which matches
        # the parent UI's chip-per-segment rendering.
        annotation = None
        for attr in ('exclusive_speaker_diarization', 'speaker_diarization'):
            cand = getattr(result, attr, None)
            if cand is not None and hasattr(cand, 'itertracks'):
                annotation = cand
                break
        if annotation is None and hasattr(result, 'itertracks'):
            annotation = result

        if annotation is None:
            # No diarization possible (e.g. all-silence audio). Return an
            # empty turns list instead of erroring — the parent caller will
            # render the transcript with no speaker labels, which is the
            # correct degraded-mode behaviour.
            _write_response(work_dir, {
                'ok': True,
                'turns': [],
                'note': (
                    f'pyannote.{type(result).__name__} has no diarization data '
                    f'(attrs available: {[a for a in dir(result) if not a.startswith("_")][:20]})'
                ),
            })
            return 0

        turns = []
        for turn, _, label in annotation.itertracks(yield_label=True):
            turns.append([float(turn.start), float(turn.end), str(label)])
        turns.sort(key=lambda t: t[0])

        _write_response(work_dir, {'ok': True, 'turns': turns})
        return 0

    except SystemExit:
        raise
    except BaseException as exc:
        try:
            _write_response(work_dir, {
                'ok': False,
                'error': f'{type(exc).__name__}: {exc}',
                'tb': traceback.format_exc(),
            })
        except Exception:
            # Worker can't even write output.json; print to stderr so
            # subprocess.run's captured stderr still has SOMETHING.
            try:
                print(traceback.format_exc(), file=sys.stderr)
            except Exception:
                pass
        return 0  # parent reads output.json, no need for nonzero exit


if __name__ == '__main__':
    sys.exit(main())
