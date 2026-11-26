from jobservice.sources.dou import Dou
from jobservice.sources.jobscz import JobsCz
from jobservice.sources.startupjobs import StartupJobs

SOURCES = {JobsCz.name: JobsCz, StartupJobs.name: StartupJobs, Dou.name: Dou}

__all__ = ["SOURCES", "Dou", "JobsCz", "StartupJobs"]
