import json
import os
import re
from datetime import datetime, timedelta, timezone, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson.json_util import dumps as bson_dumps
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError


load_dotenv()

client = MongoClient(
    os.getenv(
        "MONGO_URI",
        "mongodb://localhost:27017"
    )
)

db = client[
    os.getenv(
        "MONGO_DB",
        "attendance_db"
    )
]

UTC = timezone.utc

IST = timezone(
    timedelta(
        hours=5,
        minutes=30
    )
)

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0"
)


PRESENCE_STATUSES = [
    "PRESENT",
    "WFH",
    "ON_DUTY"
]

ALL_STATUSES = [
    "PRESENT",
    "WFH",
    "ON_DUTY",
    "ABSENT",
    "LEAVE"
]

ENDPOINT_NAMES = [
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend"
]


@app.on_event("startup")
def create_indexes():

    db.employees.create_index(
        "emp_code",
        unique=True
    )

    db.employees.create_index(
        [
            ("department", 1),
            ("emp_code", 1)
        ]
    )

    db.employees.create_index(
        "joined_on"
    )

    db.employees.create_index(
        [
            ("department", 1),
            ("joined_on", 1),
            ("emp_code", 1)
        ]
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", 1),
            ("date", 1)
        ],
        unique=True
    )

    db.attendance_logs.create_index(
        [
            ("date", -1),
            ("emp_code", 1)
        ]
    )


def validate_date_string(
    value: str,
    field_name: str = "date"
):
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}",
        value
    ):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be YYYY-MM-DD"
        )

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d"
        ).date()

    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be YYYY-MM-DD"
        )


def validate_month(
    month: str
):
    if not re.fullmatch(
        r"\d{4}-(0[1-9]|1[0-2])",
        month
    ):
        raise HTTPException(
            status_code=422,
            detail="month must be YYYY-MM"
        )

    year = int(month[:4])
    month_number = int(month[5:7])

    return year, month_number


def validate_shift_time(
    value: str,
    field_name: str
):
    if not re.fullmatch(
        r"([01]\d|2[0-3]):[0-5]\d",
        value
    ):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be HH:MM"
        )


def normalize_utc(
    dt: Optional[datetime]
) -> Optional[datetime]:

    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(
            tzinfo=UTC
        )

    return dt.astimezone(UTC)


def epoch_to_datetime(
    value: int
) -> datetime:

    return datetime.fromtimestamp(
        value / 1000,
        tz=UTC
    ).replace(
        microsecond=0
    )


def datetime_to_epoch(
    dt: Optional[datetime]
) -> Optional[int]:

    if dt is None:
        return None

    dt = normalize_utc(dt)

    return int(
        dt.timestamp() * 1000
    )


def attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str
) -> str:

    punch_in = normalize_utc(
        punch_in
    )

    local = punch_in.astimezone(
        IST
    )

    start_h, start_m = map(
        int,
        shift_start.split(":")
    )

    end_h, end_m = map(
        int,
        shift_end.split(":")
    )

    is_overnight = (
        end_h,
        end_m
    ) <= (
        start_h,
        start_m
    )

    if is_overnight:

        current_time = (
            local.hour,
            local.minute,
            local.second
        )

        if current_time < (
            end_h,
            end_m,
            0
        ):
            return (
                local.date()
                - timedelta(days=1)
            ).isoformat()

    return local.date().isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_day: str
) -> int:

    punch_in = normalize_utc(
        punch_in
    )

    h, m = map(
        int,
        shift_start.split(":")
    )

    start = datetime.fromisoformat(
        attendance_day
    ).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    punch_in_ist = punch_in.astimezone(
        IST
    )

    minutes = int(
        (
            punch_in_ist - start
        ).total_seconds()
        / 60
    )

    if minutes > 10:
        return minutes

    return 0


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:

    punch_in = normalize_utc(
        punch_in
    )

    punch_out = normalize_utc(
        punch_out
    )

    seconds = int(
        (
            punch_out - punch_in
        ).total_seconds()
    )

    hours = (
        Decimal(seconds)
        / Decimal(3600)
    )

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:

    punch_out = normalize_utc(
        punch_out
    )

    start_h, start_m = map(
        int,
        shift_start.split(":")
    )

    end_h, end_m = map(
        int,
        shift_end.split(":")
    )

    shift_end_dt = datetime.fromisoformat(
        date_str
    ).replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    is_overnight = (
        end_h,
        end_m
    ) <= (
        start_h,
        start_m
    )

    if is_overnight:
        shift_end_dt += timedelta(
            days=1
        )

    punch_out_ist = punch_out.astimezone(
        IST
    )

    overtime = int(
        (
            punch_out_ist - shift_end_dt
        ).total_seconds()
        / 60
    )

    if overtime >= 30:
        return overtime

    return 0


def month_dates(
    month: str
):

    year, month_number = validate_month(
        month
    )

    start_date = datetime(
        year,
        month_number,
        1,
        tzinfo=IST
    ).date()

    if month_number == 12:

        next_month = datetime(
            year + 1,
            1,
            1,
            tzinfo=IST
        ).date()

    else:

        next_month = datetime(
            year,
            month_number + 1,
            1,
            tzinfo=IST
        ).date()

    end_date = (
        next_month
        - timedelta(days=1)
    )

    return (
        start_date,
        end_date
    )


def attendance_response(
    doc: dict
) -> dict:

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch(
            doc["punch_in"]
        ),
        "punch_out": datetime_to_epoch(
            doc["punch_out"]
        ),
        "work_hours": doc["work_hours"],
        "late_minutes": doc["late_minutes"],
        "overtime_minutes": doc[
            "overtime_minutes"
        ],
        "half_day": doc["half_day"],
        "history": doc["history"]
    }


class EmployeeIn(BaseModel):

    emp_code: str = Field(
        min_length=5,
        max_length=9,
        pattern=r"^EMP\d{4,6}$"
    )

    name: str = Field(
        min_length=1,
        max_length=100
    )

    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )

    department: str = Field(
        min_length=1,
        max_length=50
    )

    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )

    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )

    joined_on: str


class PunchInIn(BaseModel):

    emp_code: str

    punched_at: Optional[
        StrictInt
    ] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

    status: Literal[
        "PRESENT",
        "WFH",
        "ON_DUTY"
    ] = "PRESENT"


class PunchOutIn(BaseModel):

    emp_code: str

    punched_at: Optional[
        StrictInt
    ] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )


class RegularizeIn(BaseModel):

    status: Optional[
        Literal[
            "PRESENT",
            "WFH",
            "ON_DUTY",
            "ABSENT",
            "LEAVE"
        ]
    ] = None

    punch_in: Optional[
        StrictInt
    ] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

    punch_out: Optional[
        StrictInt
    ] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

    reason: str = Field(
        min_length=5,
        max_length=200
    )

    regularized_by: str = Field(
        min_length=1,
        max_length=50
    )


@app.get("/health")
def health():

    try:

        client.admin.command(
            "ping"
        )

        return {
            "status": "ok"
        }

    except Exception:

        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable"
        )


@app.post(
    "/employees",
    status_code=201
)
def create_employee(
    body: EmployeeIn
):

    validate_date_string(
        body.joined_on,
        "joined_on"
    )

    validate_shift_time(
        body.shift_start,
        "shift_start"
    )

    validate_shift_time(
        body.shift_end,
        "shift_end"
    )

    if body.shift_start == body.shift_end:

        raise HTTPException(
            status_code=422,
            detail="shift_start must differ from shift_end"
        )

    doc = body.model_dump()

    created_at = datetime.now(
        UTC
    ).replace(
        microsecond=0
    )

    doc["created_at"] = created_at

    try:

        db.employees.insert_one(
            doc
        )

    except DuplicateKeyError:

        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc.pop(
        "_id",
        None
    )

    doc["created_at"] = datetime_to_epoch(
        created_at
    )

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(
        1,
        ge=1
    ),
    page_size: int = Query(
        20,
        ge=1,
        le=100
    )
):

    query = {}

    if department is not None:

        query["department"] = department

    skip = (
        page - 1
    ) * page_size

    total = db.employees.count_documents(
        query
    )

    items = list(
        db.employees
        .find(
            query,
            {"_id": 0}
        )
        .sort(
            "emp_code",
            1
        )
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post(
    "/attendance/punch-in",
    status_code=201
)
def punch_in(
    body: PunchInIn
):

    emp = db.employees.find_one({
        "emp_code": body.emp_code
    })

    if emp is None:

        raise HTTPException(
            status_code=404,
            detail="employee not found"
        )

    if body.punched_at is None:

        timestamp = datetime.now(
            UTC
        ).replace(
            microsecond=0
        )

    else:

        timestamp = epoch_to_datetime(
            body.punched_at
        )

    attendance_day = attendance_date(
        timestamp,
        emp["shift_start"],
        emp["shift_end"]
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": timestamp,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            timestamp,
            emp["shift_start"],
            attendance_day
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:

        db.attendance_logs.insert_one(
            doc
        )

    except DuplicateKeyError:

        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    return attendance_response(
        doc
    )


@app.post(
    "/attendance/punch-out"
)
def punch_out(
    body: PunchOutIn
):

    emp = db.employees.find_one({
        "emp_code": body.emp_code
    })

    if emp is None:

        raise HTTPException(
            status_code=404,
            detail="employee not found"
        )

    if body.punched_at is None:

        timestamp = datetime.now(
            UTC
        ).replace(
            microsecond=0
        )

    else:

        timestamp = epoch_to_datetime(
            body.punched_at
        )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {
                "$lte": timestamp
            }
        },
        sort=[
            ("punch_in", -1)
        ]
    )

    if record is None:

        raise HTTPException(
            status_code=404,
            detail="no punch-in found"
        )

    punch_in_dt = normalize_utc(
        record["punch_in"]
    )

    if record["punch_out"] is not None:

        raise HTTPException(
            status_code=409,
            detail="already punched out"
        )

    if timestamp <= punch_in_dt:

        raise HTTPException(
            status_code=422,
            detail="punch-out must be after punch-in"
        )

    if timestamp > (
        punch_in_dt
        + timedelta(hours=24)
    ):

        raise HTTPException(
            status_code=422,
            detail=(
                "punch-out cannot be more "
                "than 24 hours after punch-in"
            )
        )

    work_hours = compute_work_hours(
        punch_in_dt,
        timestamp
    )

    overtime_minutes = compute_overtime(
        timestamp,
        emp["shift_start"],
        emp["shift_end"],
        record["date"]
    )

    half_day = (
        work_hours < 4.50
    )

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": timestamp,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )

    if result.matched_count == 0:

        raise HTTPException(
            status_code=409,
            detail="already punched out"
        )

    updated = db.attendance_logs.find_one(
        {
            "_id": record["_id"]
        }
    )

    return attendance_response(
        updated
    )


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str]
):

    if date_from is not None:
        validate_date_string(
            date_from,
            "date_from"
        )

    if date_to is not None:
        validate_date_string(
            date_to,
            "date_to"
        )

    if (
        date_from is not None
        and date_to is not None
        and date_from > date_to
    ):

        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to"
        )

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:

        query["date"] = {}

        if date_from is not None:
            query["date"]["$gte"] = date_from

        if date_to is not None:
            query["date"]["$lte"] = date_to

    if status is not None:
        query["status"] = status

    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal[
            "PRESENT",
            "WFH",
            "ON_DUTY",
            "ABSENT",
            "LEAVE"
        ]
    ] = None,
    page: int = Query(
        1,
        ge=1
    ),
    page_size: int = Query(
        20,
        ge=1,
        le=100
    )
):

    query = build_attendance_query(
        emp_code,
        date_from,
        date_to,
        status
    )

    total = db.attendance_logs.count_documents(
        query
    )

    skip = (
        page - 1
    ) * page_size

    docs = list(
        db.attendance_logs
        .find(query)
        .sort(
            [
                ("date", -1),
                ("emp_code", 1)
            ]
        )
        .skip(skip)
        .limit(page_size)
    )

    items = [
        attendance_response(doc)
        for doc in docs
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch(
    "/attendance/{emp_code}/{date}"
)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):

    validate_date_string(
        date,
        "date"
    )

    emp = db.employees.find_one({
        "emp_code": emp_code
    })

    if emp is None:

        raise HTTPException(
            status_code=404,
            detail="employee not found"
        )

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if record is None:

        raise HTTPException(
            status_code=404,
            detail="attendance record not found"
        )

    final_status = (
        body.status
        if body.status is not None
        else record["status"]
    )

    if final_status in (
        "ABSENT",
        "LEAVE"
    ):

        if (
            body.punch_in is not None
            or body.punch_out is not None
        ):

            raise HTTPException(
                status_code=422,
                detail=(
                    "ABSENT/LEAVE cannot have "
                    "punch times"
                )
            )

        final_punch_in = None
        final_punch_out = None

    else:

        final_punch_in = (
            epoch_to_datetime(
                body.punch_in
            )
            if body.punch_in is not None
            else normalize_utc(
                record["punch_in"]
            )
        )

        final_punch_out = (
            epoch_to_datetime(
                body.punch_out
            )
            if body.punch_out is not None
            else normalize_utc(
                record["punch_out"]
            )
        )

        if final_punch_in is None:

            raise HTTPException(
                status_code=422,
                detail=(
                    "presence status requires "
                    "punch_in"
                )
            )

        punch_in_local_date = (
            final_punch_in
            .astimezone(IST)
            .date()
            .isoformat()
        )

        if punch_in_local_date != date:

            raise HTTPException(
                status_code=422,
                detail=(
                    "punch_in must belong to "
                    "attendance date"
                )
            )

        if final_punch_out is not None:

            if final_punch_out <= final_punch_in:

                raise HTTPException(
                    status_code=422,
                    detail=(
                        "punch_out must be "
                        "after punch_in"
                    )
                )

            if final_punch_out > (
                final_punch_in
                + timedelta(hours=24)
            ):

                raise HTTPException(
                    status_code=422,
                    detail=(
                        "punch_out cannot be more "
                        "than 24 hours after punch_in"
                    )
                )

    if final_punch_in is None:

        final_late_minutes = 0

    else:

        final_late_minutes = compute_late_minutes(
            final_punch_in,
            emp["shift_start"],
            date
        )

    if final_punch_out is None:

        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False

    else:

        final_work_hours = compute_work_hours(
            final_punch_in,
            final_punch_out
        )

        final_overtime_minutes = compute_overtime(
            final_punch_out,
            emp["shift_start"],
            emp["shift_end"],
            date
        )

        final_half_day = (
            final_work_hours < 4.50
        )

    old_punch_in = normalize_utc(
        record["punch_in"]
    )

    old_punch_out = normalize_utc(
        record["punch_out"]
    )

    old_values = {
        "status": record["status"],
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": record["work_hours"],
        "late_minutes": record["late_minutes"],
        "overtime_minutes": record[
            "overtime_minutes"
        ],
        "half_day": record["half_day"]
    }

    new_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day
    }

    changed_fields = {}

    for field_name in new_values:

        if (
            old_values[field_name]
            != new_values[field_name]
        ):

            old_value = old_values[
                field_name
            ]

            new_value = new_values[
                field_name
            ]

            if field_name in (
                "punch_in",
                "punch_out"
            ):

                old_value = datetime_to_epoch(
                    old_value
                )

                new_value = datetime_to_epoch(
                    new_value
                )

            changed_fields[
                field_name
            ] = {
                "from": old_value,
                "to": new_value
            }

    if not changed_fields:

        raise HTTPException(
            status_code=422,
            detail="no changes supplied"
        )

    history_entry = {
        "at": datetime_to_epoch(
            datetime.now(
                UTC
            ).replace(
                microsecond=0
            )
        ),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changed_fields
    }

    update_result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": old_punch_in,
            "punch_out": old_punch_out,
            "work_hours": record["work_hours"],
            "late_minutes": record[
                "late_minutes"
            ],
            "overtime_minutes": record[
                "overtime_minutes"
            ],
            "half_day": record["half_day"]
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {
                "history": history_entry
            }
        }
    )

    if update_result.matched_count == 0:

        raise HTTPException(
            status_code=409,
            detail=(
                "attendance record was modified "
                "by another request"
            )
        )

    updated = db.attendance_logs.find_one({
        "_id": record["_id"]
    })

    return attendance_response(
        updated
    )


def build_employee_monthly_pipeline(
    emp_code: str,
    start_date,
    end_date
):

    return [

        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat()
                }
            }
        },

        {
            "$group": {

                "_id": None,

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            PRESENCE_STATUSES
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [
                                                2,
                                                3,
                                                4,
                                                5,
                                                6
                                            ]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    "$half_day",
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$status",
                                    "LEAVE"
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$late_minutes",
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },

                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                }
            }
        }
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly"
)
def employee_monthly(
    emp_code: str,
    month: str
):

    emp = db.employees.find_one({
        "emp_code": emp_code
    })

    if emp is None:

        raise HTTPException(
            status_code=404,
            detail="employee not found"
        )

    start_date, end_date = month_dates(
        month
    )

    joined_on = validate_date_string(
        emp["joined_on"],
        "joined_on"
    )

    first_day = max(
        start_date,
        joined_on
    )

    working_days = 0

    current = first_day

    while current <= end_date:

        if current.weekday() < 5:
            working_days += 1

        current += timedelta(
            days=1
        )

    pipeline = build_employee_monthly_pipeline(
        emp_code,
        start_date,
        end_date
    )

    result = list(
        db.attendance_logs.aggregate(
            pipeline
        )
    )

    if result:

        data = result[0]

        present_days = data[
            "present_days"
        ]

        leave_days = data[
            "leave_days"
        ]

        late_count = data[
            "late_count"
        ]

        total_late_minutes = data[
            "total_late_minutes"
        ]

        total_overtime_minutes = data[
            "total_overtime_minutes"
        ]

    else:

        present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:

        attendance_pct = None

    else:

        attendance_pct = float(
            (
                Decimal(
                    str(present_days)
                )
                / Decimal(
                    str(working_days)
                )
                * Decimal("100")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


def build_department_summary_pipeline(
    start_date,
    end_date,
    department: Optional[str]
):

    employee_match = {
        "joined_on": {
            "$lte": end_date.isoformat()
        }
    }

    if department is not None:

        employee_match[
            "department"
        ] = department

    return [

        {
            "$match": employee_match
        },

        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code"
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            start_date.isoformat()
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            end_date.isoformat()
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance"
            }
        },

        {
            "$unwind": {
                "path": "$attendance",
                "preserveNullAndEmptyArrays": True
            }
        },

        {
            "$group": {

                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code"
                },

                "employee_present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            PRESENCE_STATUSES
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString":
                                                            "$attendance.date"
                                                    }
                                                }
                                            },
                                            [
                                                2,
                                                3,
                                                4,
                                                5,
                                                6
                                            ]
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    "$attendance.half_day",
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            PRESENCE_STATUSES
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$attendance.work_hours",
                                            None
                                        ]
                                    }
                                ]
                            },
                            "$attendance.work_hours",
                            0
                        ]
                    }
                },

                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$attendance.status",
                                            PRESENCE_STATUSES
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$attendance.work_hours",
                                            None
                                        ]
                                    }
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$attendance.late_minutes",
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$attendance.late_minutes",
                                    0
                                ]
                            },
                            "$attendance.late_minutes",
                            0
                        ]
                    }
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$attendance.status",
                                    "LEAVE"
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$attendance.status",
                                    "ON_DUTY"
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },

        {
            "$group": {

                "_id": "$_id.department",

                "headcount": {
                    "$sum": 1
                },

                "present_days": {
                    "$sum": "$employee_present_days"
                },

                "work_hours_sum": {
                    "$sum": "$work_hours_sum"
                },

                "work_hours_count": {
                    "$sum": "$work_hours_count"
                },

                "late_count": {
                    "$sum": "$late_count"
                },

                "total_late_minutes": {
                    "$sum": "$total_late_minutes"
                },

                "leave_count": {
                    "$sum": "$leave_count"
                },

                "on_duty_count": {
                    "$sum": "$on_duty_count"
                }
            }
        },

        {
            "$project": {

                "_id": 0,

                "department": "$_id",

                "headcount": 1,

                "present_days": 1,

                "avg_work_hours_raw": {
                    "$cond": [
                        {
                            "$gt": [
                                "$work_hours_count",
                                0
                            ]
                        },
                        {
                            "$divide": [
                                "$work_hours_sum",
                                "$work_hours_count"
                            ]
                        },
                        None
                    ]
                },

                "late_count": 1,

                "total_late_minutes": 1,

                "leave_count": 1,

                "on_duty_count": 1
            }
        },

        {
            "$sort": {
                "department": 1
            }
        }
    ]


@app.get(
    "/analytics/departments/summary"
)
def department_summary(
    month: str,
    department: Optional[str] = None
):

    start_date, end_date = month_dates(
        month
    )

    pipeline = build_department_summary_pipeline(
        start_date,
        end_date,
        department
    )

    rows = list(
        db.employees.aggregate(
            pipeline
        )
    )

    output = []

    for row in rows:

        raw_avg = row.pop(
            "avg_work_hours_raw",
            None
        )

        if raw_avg is None:

            avg_work_hours = None

        else:

            avg_work_hours = float(
                Decimal(
                    str(raw_avg)
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

        row["avg_work_hours"] = (
            avg_work_hours
        )

        output.append(row)

    return {
        "month": month,
        "items": output
    }


def build_late_leaderboard_pipeline(
    start_date,
    end_date,
    limit: int,
    department: Optional[str]
):

    pipeline = [

        {
            "$match": {
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat()
                },
                "late_minutes": {
                    "$gt": 0
                }
            }
        },

        {
            "$group": {

                "_id": "$emp_code",

                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },

                "late_count": {
                    "$sum": 1
                }
            }
        },

        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },

        {
            "$unwind": "$employee"
        }
    ]

    if department is not None:

        pipeline.append(
            {
                "$match": {
                    "employee.department": department
                }
            }
        )

    pipeline.extend(
        [

            {
                "$setWindowFields": {

                    "sortBy": {
                        "total_late_minutes": -1
                    },

                    "output": {
                        "rank": {
                            "$rank": {}
                        }
                    }
                }
            },

            {
                "$match": {
                    "rank": {
                        "$lte": limit
                    }
                }
            },

            {
                "$project": {

                    "_id": 0,

                    "rank": 1,

                    "emp_code": "$_id",

                    "name": "$employee.name",

                    "department":
                        "$employee.department",

                    "total_late_minutes": 1,

                    "late_count": 1
                }
            },

            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1
                }
            }
        ]
    )

    return pipeline


@app.get(
    "/analytics/leaderboard/late"
)
def late_leaderboard(
    month: str,
    limit: int = Query(
        10,
        ge=1,
        le=50
    ),
    department: Optional[str] = None
):

    start_date, end_date = month_dates(
        month
    )

    pipeline = build_late_leaderboard_pipeline(
        start_date,
        end_date,
        limit,
        department
    )

    items = list(
        db.attendance_logs.aggregate(
            pipeline
        )
    )

    return {
        "month": month,
        "items": items
    }


def build_department_trend_pipeline(
    department: str,
    start_date,
    end_date
):

    return [

        {
            "$match": {
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lte": end_date.isoformat()
                }
            }
        },

        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },

        {
            "$unwind": "$employee"
        },

        {
            "$match": {
                "employee.department": department
            }
        },

        {
            "$set": {
                "date_value": {
                    "$dateFromString": {
                        "dateString": "$date"
                    }
                }
            }
        },

        {
            "$group": {

                "_id": "$date_value",

                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    PRESENCE_STATUSES
                                ]
                            },
                            {
                                "$cond": [
                                    "$half_day",
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    "$late_minutes",
                                    0
                                ]
                            },
                            1,
                            0
                        ]
                    }
                }
            }
        },

        {
            "$project": {

                "_id": 0,

                "date": "$_id",

                "present_count": 1,

                "late_count": 1
            }
        },

        {
            "$densify": {

                "field": "date",

                "partitionByFields": [],

                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(
                            start_date,
                            time.min
                        ),
                        datetime.combine(
                            end_date
                            + timedelta(days=1),
                            time.min
                        )
                    ]
                }
            }
        },

        {
            "$set": {

                "present_count": {
                    "$ifNull": [
                        "$present_count",
                        0
                    ]
                },

                "late_count": {
                    "$ifNull": [
                        "$late_count",
                        0
                    ]
                }
            }
        },

        {
            "$set": {

                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date"
                    }
                }
            }
        },

        {
            "$lookup": {

                "from": "employees",

                "let": {
                    "current_day": "$date_string"
                },

                "pipeline": [

                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": [
                                    "$joined_on",
                                    "$$current_day"
                                ]
                            }
                        }
                    },

                    {
                        "$project": {
                            "_id": 1
                        }
                    }
                ],

                "as": "headcount_employees"
            }
        },

        {
            "$set": {

                "headcount": {
                    "$size": "$headcount_employees"
                }
            }
        },

        {
            "$set": {

                "is_working_day": {
                    "$in": [
                        {
                            "$dayOfWeek": "$date"
                        },
                        [
                            2,
                            3,
                            4,
                            5,
                            6
                        ]
                    ]
                }
            }
        },

        {
            "$set": {

                "attendance_rate_raw": {
                    "$cond": [

                        {
                            "$and": [
                                "$is_working_day",
                                {
                                    "$gt": [
                                        "$headcount",
                                        0
                                    ]
                                }
                            ]
                        },

                        {
                            "$divide": [
                                "$present_count",
                                "$headcount"
                            ]
                        },

                        None
                    ]
                }
            }
        },

        {
            "$set": {

                "attendance_rate": {
                    "$cond": [

                        {
                            "$ne": [
                                "$attendance_rate_raw",
                                None
                            ]
                        },

                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate_raw",
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },

                        None
                    ]
                }
            }
        },

        {
            "$setWindowFields": {

                "sortBy": {
                    "date": 1
                },

                "output": {

                    "moving_avg_raw": {

                        "$avg": "$attendance_rate",

                        "window": {
                            "documents": [
                                -6,
                                0
                            ]
                        }
                    }
                }
            }
        },

        {
            "$set": {

                "moving_avg_7d": {

                    "$cond": [

                        {
                            "$ne": [
                                "$moving_avg_raw",
                                None
                            ]
                        },

                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000
                                                ]
                                            },
                                            0.5
                                        ]
                                    }
                                },
                                10000
                            ]
                        },

                        None
                    ]
                }
            }
        },

        {
            "$project": {

                "_id": 0,

                "date": "$date_string",

                "is_working_day": 1,

                "headcount": 1,

                "present_count": 1,

                "late_count": 1,

                "attendance_rate": 1,

                "moving_avg_7d": 1
            }
        },

        {
            "$sort": {
                "date": 1
            }
        }
    ]


@app.get(
    "/analytics/departments/{department}/trend"
)
def department_trend(
    department: str,
    from_date: str = Query(
        ...,
        alias="from"
    ),
    to_date: str = Query(
        ...,
        alias="to"
    )
):

    start_date = validate_date_string(
        from_date,
        "from"
    )

    end_date = validate_date_string(
        to_date,
        "to"
    )

    if end_date < start_date:

        raise HTTPException(
            status_code=422,
            detail=(
                "to must be greater than "
                "or equal to from"
            )
        )

    if (
        end_date - start_date
    ).days + 1 > 92:

        raise HTTPException(
            status_code=422,
            detail=(
                "date range cannot exceed "
                "92 days"
            )
        )

    department_exists = db.employees.find_one(
        {
            "department": department
        },
        {
            "_id": 1
        }
    )

    if department_exists is None:

        raise HTTPException(
            status_code=404,
            detail="department not found"
        )

    pipeline = build_department_trend_pipeline(
        department,
        start_date,
        end_date
    )

    items = list(
        db.attendance_logs.aggregate(
            pipeline
        )
    )

    return {
        "department": department,
        "items": items
    }


def explain_aggregate(
    collection_name: str,
    pipeline: list
):

    command = {
        "explain": {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {}
        },
        "verbosity": "executionStats"
    }

    result = db.command(
        command
    )

    return json.loads(
        bson_dumps(result)
    )


def explain_find(
    collection_name: str,
    query: dict,
    page: int,
    page_size: int
):

    skip = (
        page - 1
    ) * page_size

    result = db.command(
        {
            "explain": {
                "find": collection_name,
                "filter": query,
                "sort": {
                    "date": -1,
                    "emp_code": 1
                },
                "skip": skip,
                "limit": page_size
            },
            "verbosity": "executionStats"
        }
    )

    return json.loads(
        bson_dumps(result)
    )


@app.get(
    "/admin/explain/{endpoint}"
)
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(
        10,
        ge=1,
        le=50
    ),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(
        None,
        alias="from"
    ),
    to_date: Optional[str] = Query(
        None,
        alias="to"
    ),
    page: int = Query(
        1,
        ge=1
    ),
    page_size: int = Query(
        20,
        ge=1,
        le=100
    )
):

    if endpoint == "attendance_list":

        query = build_attendance_query(
            emp_code,
            date_from,
            date_to,
            None
        )

        explain = explain_find(
            "attendance_logs",
            query,
            page,
            page_size
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    if endpoint == "employee_monthly":

        if emp_code is None:

            raise HTTPException(
                status_code=422,
                detail="emp_code is required"
            )

        if month is None:

            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        start_date, end_date = month_dates(
            month
        )

        employee = db.employees.find_one(
            {
                "emp_code": emp_code
            },
            {
                "_id": 1
            }
        )

        if employee is None:

            raise HTTPException(
                status_code=404,
                detail="employee not found"
            )

        pipeline = build_employee_monthly_pipeline(
            emp_code,
            start_date,
            end_date
        )

        explain = explain_aggregate(
            "attendance_logs",
            pipeline
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    if endpoint == "department_summary":

        if month is None:

            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        start_date, end_date = month_dates(
            month
        )

        pipeline = build_department_summary_pipeline(
            start_date,
            end_date,
            department
        )

        explain = explain_aggregate(
            "employees",
            pipeline
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain
        }

    if endpoint == "late_leaderboard":

        if month is None:

            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        start_date, end_date = month_dates(
            month
        )

        pipeline = build_late_leaderboard_pipeline(
            start_date,
            end_date,
            limit,
            department
        )

        explain = explain_aggregate(
            "attendance_logs",
            pipeline
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    if endpoint == "department_trend":

        if department is None:

            raise HTTPException(
                status_code=422,
                detail="department is required"
            )

        if from_date is None:

            raise HTTPException(
                status_code=422,
                detail="from is required"
            )

        if to_date is None:

            raise HTTPException(
                status_code=422,
                detail="to is required"
            )

        start_date = validate_date_string(
            from_date,
            "from"
        )

        end_date = validate_date_string(
            to_date,
            "to"
        )

        if end_date < start_date:

            raise HTTPException(
                status_code=422,
                detail=(
                    "to must be greater than "
                    "or equal to from"
                )
            )

        if (
            end_date - start_date
        ).days + 1 > 92:

            raise HTTPException(
                status_code=422,
                detail=(
                    "date range cannot exceed "
                    "92 days"
                )
            )

        department_exists = db.employees.find_one(
            {
                "department": department
            },
            {
                "_id": 1
            }
        )

        if department_exists is None:

            raise HTTPException(
                status_code=404,
                detail="department not found"
            )

        pipeline = build_department_trend_pipeline(
            department,
            start_date,
            end_date
        )

        explain = explain_aggregate(
            "attendance_logs",
            pipeline
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain
        }

    raise HTTPException(
        status_code=422,
        detail="unsupported endpoint"
    )