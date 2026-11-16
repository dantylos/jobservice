from collections.abc import Iterator
from typing import Protocol

from jobservice.models import SearchFilter, Vacancy


class Source(Protocol):
    name: str

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        """Yield vacancies matching the filter.

        `skip_detail` holds external ids whose description is already stored;
        for those the detail page is not fetched and description/date stay None.
        """
        ...
