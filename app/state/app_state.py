from dataclasses import dataclass, field
from typing import List
import asyncio


@dataclass
class OndemandState:
    thresholdUp: int = 0
    samplingRate: int = 0
    samplingDownFactor: int = 0
    isIgnoreNice: bool = False
    isIoBusy: bool = False
    powerBias: int = 0


@dataclass
class ConservativeState:
    thresholdUp: int = 0
    thresholdDown: int = 0
    samplingRate: int = 0
    samplingDownFactor: int = 0
    isIgnoreNice: bool = False
    frequencyStep: int = 0


@dataclass
class SchedutilState:
    rateLimit: int = 0


@dataclass
class UserspaceState:
    fixedFrequency: float = 0
    isDynamicScripting: bool = False
    script: str = ""


@dataclass
class CPUState:
    governor: str = "ondemand"
    maxFreq: float = 0
    minFreq: float = 0

    # Menghubungkan semua state governor
    ondemand: OndemandState = field(default_factory=OndemandState)
    conservative: ConservativeState = field(default_factory=ConservativeState)
    schedutil: SchedutilState = field(default_factory=SchedutilState)
    userspace: UserspaceState = field(default_factory=UserspaceState)


@dataclass
class ModelState:
    num_threads: int = 4
    cores: List[int] = field(default_factory=lambda: [0, 1, 2, 3])
    process_pid: int = 0
    reload_model_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

@dataclass
class AppState:
    cpu: CPUState = field(default_factory=CPUState)
    model: ModelState = field(default_factory=ModelState)


# Inisialisasi objek utama
app_state = AppState()
