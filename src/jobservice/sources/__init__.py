from jobservice.sources.djinni import Djinni
from jobservice.sources.dou import Dou
from jobservice.sources.jobscz import JobsCz
from jobservice.sources.startupjobs import StartupJobs

SOURCES = {JobsCz.name: JobsCz, StartupJobs.name: StartupJobs, Dou.name: Dou, Djinni.name: Djinni}

__all__ = ["SOURCES", "Djinni", "Dou", "JobsCz", "StartupJobs"]
