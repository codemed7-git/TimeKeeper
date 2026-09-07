"""Generate 15 short built-in timer alarm WAVs (stdlib only)."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 22050
AMP = 22000


def clamp(v: float) -> int:
    return max(-32767, min(32767, int(v)))


def write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(b"".join(struct.pack("<h", clamp(s * AMP)) for s in samples))


def env(i: int, n: int, attack: float = 0.01, release: float = 0.08) -> float:
    a = int(RATE * attack)
    r = int(RATE * release)
    if i < a:
        return i / max(1, a)
    if i > n - r:
        return max(0.0, (n - i) / max(1, r))
    return 1.0


def tone(freq: float, seconds: float, kind: str = "sine") -> list[float]:
    n = int(RATE * seconds)
    out = []
    for i in range(n):
        t = i / RATE
        if kind == "square":
            v = 1.0 if math.sin(2 * math.pi * freq * t) >= 0 else -1.0
            v *= 0.35
        elif kind == "triangle":
            v = 2 * abs(2 * ((t * freq) % 1) - 1) - 1
            v *= 0.55
        else:
            v = math.sin(2 * math.pi * freq * t)
        out.append(v * env(i, n))
    return out


def silence(seconds: float) -> list[float]:
    return [0.0] * int(RATE * seconds)


def sweep(f0: float, f1: float, seconds: float) -> list[float]:
    n = int(RATE * seconds)
    out = []
    phase = 0.0
    for i in range(n):
        freq = f0 + (f1 - f0) * (i / max(1, n - 1))
        phase += 2 * math.pi * freq / RATE
        out.append(math.sin(phase) * env(i, n, 0.02, 0.12))
    return out


def decay_tone(freq: float, seconds: float, harmonics: tuple[float, ...] = (1.0,)) -> list[float]:
    n = int(RATE * seconds)
    out = []
    for i in range(n):
        t = i / RATE
        v = 0.0
        for h, w in enumerate(harmonics, start=1):
            v += w * math.sin(2 * math.pi * freq * h * t)
        fade = math.exp(-3.2 * t / max(0.05, seconds))
        out.append(v * fade * env(i, n, 0.005, 0.04))
    peak = max((abs(x) for x in out), default=1.0) or 1.0
    return [x / peak for x in out]


SOUNDS = [
    ("01_pik", lambda: tone(880, 0.28)),
    ("02_dvoynoy", lambda: tone(880, 0.16) + silence(0.09) + tone(880, 0.16)),
    ("03_troynoy", lambda: tone(980, 0.1) + silence(0.07) + tone(980, 0.1) + silence(0.07) + tone(980, 0.12)),
    ("04_zvonok", lambda: decay_tone(740, 1.1, (1.0, 0.35, 0.12))),
    ("05_kolokolchik", lambda: decay_tone(1046, 1.2, (1.0, 0.45, 0.18, 0.08))),
    ("06_cifrovoy", lambda: tone(1200, 0.22, "square") + silence(0.08) + tone(900, 0.22, "square")),
    ("07_puls", lambda: sum((tone(700, 0.08) + silence(0.08) for _ in range(4)), [])),
    (
        "08_arpedzhio",
        lambda: decay_tone(523.25, 0.35, (1.0, 0.2))
        + decay_tone(659.25, 0.35, (1.0, 0.2))
        + decay_tone(783.99, 0.7, (1.0, 0.25)),
    ),
    (
        "09_sirena",
        lambda: sweep(620, 980, 0.45) + sweep(980, 620, 0.45) + sweep(620, 980, 0.45),
    ),
    ("10_vverh", lambda: sweep(420, 1100, 0.9)),
    ("11_vniz", lambda: sweep(1100, 380, 0.9)),
    (
        "12_ksilofon",
        lambda: decay_tone(659.25, 0.28, (1.0, 0.15))
        + decay_tone(830.61, 0.28, (1.0, 0.15))
        + decay_tone(987.77, 0.55, (1.0, 0.2)),
    ),
    ("13_chasy", lambda: tone(800, 0.12) + silence(0.14) + tone(600, 0.18)),
    ("14_myagkiy", lambda: tone(392, 0.7, "triangle") + tone(494, 0.85, "triangle")),
    (
        "15_srochnyy",
        lambda: sum((tone(1400, 0.06, "square") + silence(0.05) for _ in range(6)), []),
    ),
]


def main() -> None:
    here = Path(__file__).parent
    for name, factory in SOUNDS:
        write_wav(here / f"{name}.wav", factory())
        print("wrote", name)


if __name__ == "__main__":
    main()
