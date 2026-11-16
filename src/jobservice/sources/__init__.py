from jobservice.sources.jobscz import JobsCz
from jobservice.sources.startupjobs import StartupJobs

SOURCES = {JobsCz.name: JobsCz, StartupJobs.name: StartupJobs}

__all__ = ["SOURCES", "JobsCz", "StartupJobs"]
