"""Microphone diagnostic: is the problem the mic, the VAD, or Whisper?

    python -m src.stt.mictest          # capture 3 utterances, show timings
    python -m src.stt.mictest --devices  # list input devices
"""

from __future__ import annotations

import argparse
import time

from rich.console import Console

from ..config import load_config

console = Console()


def list_devices() -> None:
    import pyaudio

    pa = pyaudio.PyAudio()
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if int(info.get("maxInputChannels", 0)) > 0:
            console.print(f"  [{i}] {info['name']}")
    pa.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mic-test", description=__doc__)
    parser.add_argument("--devices", action="store_true", help="list input devices")
    args = parser.parse_args()

    if args.devices:
        console.print("input devices (set stt.input_device_index in config.yaml):")
        list_devices()
        return

    from . import make_stt_engine

    cfg = load_config()
    console.print(
        f"[dim]model={cfg['stt']['model']} beam={cfg['stt'].get('beam_size', 2)} "
        f"silence={cfg['stt'].get('silence_duration', 1.0)}s[/dim]"
    )
    console.print("[dim]loading model...[/dim]")
    stt = make_stt_engine(cfg)
    console.print("Speak three test sentences (try one starting with 'Hey Jarvis'):\n")
    try:
        for i in range(3):
            console.print(f"[bold]({i + 1}/3) listening...[/bold]")
            t0 = time.monotonic()
            text = stt.listen()
            elapsed = time.monotonic() - t0
            console.print(f'  heard: [green]"{text}"[/green]  [dim]({elapsed:.1f}s incl. your speech)[/dim]')
    except KeyboardInterrupt:
        pass
    finally:
        stt.shutdown()


if __name__ == "__main__":
    main()
