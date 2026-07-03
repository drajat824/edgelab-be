from dataclasses import dataclass, field
from typing import List

@dataclass
class OndemandState:
    thresholdUp: int = 0
    thresholdDown: int = 0
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
    governor: str = "powersave"
    maxFreq: float = 0
    minFreq: float = 0
    thread: int = 4
    core: List[int] = field(default_factory=lambda: [0, 1, 2, 3])
    
    # Menghubungkan semua state governor
    ondemand: OndemandState = field(default_factory=OndemandState)
    conservative: ConservativeState = field(default_factory=ConservativeState)
    schedutil: SchedutilState = field(default_factory=SchedutilState)
    userspace: UserspaceState = field(default_factory=UserspaceState)

@dataclass
class AppState:
    cpu: CPUState = field(default_factory=CPUState)

# Inisialisasi objek utama
app_state = AppState()