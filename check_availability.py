import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from os import linesep, getenv
from typing import List, Optional

import redis
from bs4 import BeautifulSoup
from dataclasses_json import config, dataclass_json
from dotenv import load_dotenv, find_dotenv
from marshmallow import fields
from pythonjsonlogger import jsonlogger
from retry_requests import retry

LOG_FIELDS = ["levelname"]

logger = logging.getLogger()
handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    reserved_attrs=[a for a in jsonlogger.RESERVED_ATTRS if a not in LOG_FIELDS],
    timestamp=True,
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

try:
    load_dotenv(find_dotenv(), override=True)
except Exception:
    logger.info("No .env")

USER_AGENT = getenv("USER_AGENT")
SEARCH_QUERY = getenv("SEARCH_QUERY")
BASE_URL = getenv("BASE_URL")
ZAPIER_URL = getenv("ZAPIER_URL")
REDIS_HOST = getenv("REDIS_HOST") or "localhost"
REDIS_PORT = int(getenv("REDIS_PORT") or 6379)
REDIS_KEY = getenv("REDIS_KEY") or "CCVW-clinic-tracker"
DOWN_THRESHOLD = int(getenv("DOWN_THRESHOLD") or 5)
UP_THRESHOLD = int(getenv("UP_THRESHOLD") or 25)
ALERT_INTERVAL_MINUTES = int(getenv("ALERT_INTERVAL_MINUTES ") or 60)
SLEEP_INTERVAL_SECONDS = int(getenv("SLEEP_INTERVAL_SECONDS ") or 120)


@dataclass_json
@dataclass
class ClinicData:
    name: str
    available_appointments: int
    href: Optional[str]


@dataclass_json
@dataclass
class CacheEntry:
    name: str
    available_appointments: Optional[int] = None
    href: Optional[str] = None
    alerted_up: Optional[datetime] = field(
        default=None,
        metadata=config(
            encoder=datetime.isoformat,
            decoder=datetime.fromisoformat,
            mm_field=fields.DateTime(format="iso"),
        ),
    )
    alerted_down: Optional[datetime] = field(
        default=None,
        metadata=config(
            encoder=datetime.isoformat,
            decoder=datetime.fromisoformat,
            mm_field=fields.DateTime(format="iso"),
        ),
    )


def now_minus_alert_intervals(number_of_intervals: int = 1):
    return datetime.now(timezone.utc) - timedelta(
        minutes=number_of_intervals * ALERT_INTERVAL_MINUTES
    )


def get_entry_from_redis_or_default(r, name, href=None):
    try:
        redis_old_clinic_data = r.hget(REDIS_KEY, name)
        return CacheEntry.from_json(redis_old_clinic_data)
    except Exception:
        return CacheEntry(
            name,
            href=href,
            available_appointments=0,
            alerted_up=now_minus_alert_intervals(2),
            alerted_down=now_minus_alert_intervals(2),
        )


def send_alert(alert):
    logger.info(alert)
    retry().post(ZAPIER_URL, data={"content": alert})


def get_search_page():
    headers = {"User-Agent": USER_AGENT}
    response = retry(
        status_to_retry=(302, 500, 502, 504),
    ).get(f"{BASE_URL}{SEARCH_QUERY}", headers=headers)

    if response.status_code != 200:
        raise RuntimeError(f"Got {response.status_code} from search page.")

    return response.content


def parse_search_page(content: str) -> List[ClinicData]:
    to_return = []
    soup = BeautifulSoup(content, "html.parser")
    availables = soup.find_all(string=re.compile(r".*Available Appointments.*"))
    for available in availables:
        available_appointment_match = re.match(
            r".*(\d+).*", available.parent.parent.text, flags=re.DOTALL
        )
        available_appointments = 0
        if available_appointment_match:
            available_appointments = int(available_appointment_match[1])

        name = available.parent.parent.parent.p.text.strip()
        link = available.parent.parent.parent.a
        href = link.get("href") if link else None

        to_return.append(ClinicData(name, available_appointments, href))
    return to_return


def update_cache(r, cache_entry, clinic_data):
    cache_entry.available_appointments = clinic_data.available_appointments
    cache_entry.href = clinic_data.href
    r.hset(REDIS_KEY, clinic_data.name, cache_entry.to_json())


def send_alerts_and_update_cache(clinics_data: List[ClinicData]) -> None:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    names_in_redis = set(r.hkeys(REDIS_KEY))

    for clinic_data in clinics_data:
        try:
            names_in_redis.remove(clinic_data.name)
        except KeyError:
            pass

        alert = None

        cache_entry = get_entry_from_redis_or_default(
            r, clinic_data.name, clinic_data.href
        )

        if (
            clinic_data.available_appointments >= UP_THRESHOLD
            and cache_entry.alerted_up < now_minus_alert_intervals()
        ):
            alert = (
                f"{clinic_data.available_appointments} available appointments at {clinic_data.name}!{linesep}{linesep}"
                + f"Link to register: {BASE_URL}{clinic_data.href}{linesep}{linesep}"
                + "Keep hitting refresh! It's likely appointments will become available!"
            )
            try:
                send_alert(alert)
                cache_entry.alerted_up = datetime.now(tz=timezone.utc)
                update_cache(r, cache_entry, clinic_data)
            except Exception:
                logger.error("Exception sending UP alert", exc_info=True)

        elif (
            clinic_data.available_appointments <= DOWN_THRESHOLD
            and cache_entry.available_appointments > DOWN_THRESHOLD
            and cache_entry.alerted_down < now_minus_alert_intervals()
        ):
            alert = (
                f"{clinic_data.available_appointments} available appointments at {clinic_data.name}.{linesep}{linesep}"
                + f"Unfortunately it's probably a waste of time to try to get an appointment now.{linesep}{linesep}"
                + f"If you want to try anyway: {BASE_URL}{clinic_data.href}"
            )
            try:
                send_alert(alert)
                cache_entry.alerted_down = datetime.now(tz=timezone.utc)
                update_cache(r, cache_entry, clinic_data)
            except Exception:
                logger.error("Exception sending DOWN alert", exc_info=True)

    for name in names_in_redis:
        alert = f"{clinic_data.name} removed from search results. No appointments available now."
        try:
            send_alert(alert)
            r.hdel(REDIS_KEY, name)
        except Exception:
            logger.error("Exception sending REMOVED alert", exc_info=True)


def main():
    while True:
        logger.info("Hello")
        try:
            content = get_search_page()
            clinics_data = parse_search_page(content)
            send_alerts_and_update_cache(clinics_data)
        except Exception:
            logger.error("Failed attempt!", exc_info=True)

        logger.info(f"Sleeping {SLEEP_INTERVAL_SECONDS} seconds")
        time.sleep(SLEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
