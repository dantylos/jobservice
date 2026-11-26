from jobservice.sources.djinni import Djinni
from jobservice.sources.dou import Dou
from jobservice.sources.euremotejobs import EuRemoteJobs
from jobservice.sources.jobscz import JobsCz
from jobservice.sources.nextleveljobs import NextLevelJobs
from jobservice.sources.nofluffjobs import NoFluffJobs
from jobservice.sources.startupjobs import StartupJobs

SOURCES = {
    s.name: s for s in (JobsCz, StartupJobs, Dou, Djinni, NoFluffJobs, EuRemoteJobs, NextLevelJobs)
}

__all__ = [
    "SOURCES",
    "Djinni",
    "Dou",
    "EuRemoteJobs",
    "JobsCz",
    "NextLevelJobs",
    "NoFluffJobs",
    "StartupJobs",
]
