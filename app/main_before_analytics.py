
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
    tzinfo=UTC,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
    [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
)
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
    [("emp_code", ASCENDING), ("date", ASCENDING)],
    unique=True,
    name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )


def epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")
    if value < 100_000_000_000 or value > MAX_EPOCH_MS:
        raise HTTPException(422, "Timestamp is outside the allowed epoch-millisecond range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def whole_second(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def valid_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "Date must use YYYY-MM-DD format")
    if parsed.isoformat() != value:
        raise HTTPException(422, "Date must use YYYY-MM-DD format")
    return parsed


def valid_month(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise HTTPException(422, "Month must use YYYY-MM format")
    try:
        parsed = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(422, "Invalid month")
    return parsed.year, parsed.month


def validate_shift(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Shift time must use HH:MM format")
    return value


def shift_datetime(day: date, shift: str) -> datetime:
    hour, minute = map(int, shift.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    overnight = (end_hour, end_minute) <= (start_hour, start_minute)
    if overnight and (local.hour, local.minute) < (end_hour, end_minute):
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = punch_in.astimezone(IST)
    hour, minute = map(int, shift_start.split(":"))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    seconds_late = int((local - start).total_seconds())
    if seconds_late <= 600:
        return 0
    return seconds_late // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, shift_date: date,
                     shift_start: str) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))
    end_date = shift_date
    if (end_hour, end_minute) <= (start_hour, start_minute):
        end_date += timedelta(days=1)
    shift_end_utc = shift_datetime(end_date, shift_end)
    minutes = int((punch_out - shift_end_utc).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def serialize(value):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value


def attendance_response(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    return serialize(result)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def check_shift(cls, value):
        return validate_shift(value)

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD")
        return value

    @field_validator("shift_end")
    @classmethod
    def check_end_not_same_as_start(cls, value, info):
        start = info.data.get("shift_start")
        if start and start == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    punched_at: int | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)

    
class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: int | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    punch_out: int | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = whole_second(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else whole_second(datetime.now(UTC))
    ts = whole_second(ts)
    day = attendance_day(ts, emp["shift_start"], emp["shift_end"])
    day_str = day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": day_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else whole_second(datetime.now(UTC))
    ts = whole_second(ts)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "No matching punch-in record")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    punch_in_time = whole_second(record["punch_in"])
    elapsed = (ts - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

    hours = compute_work_hours(punch_in_time, ts)
    overtime = compute_overtime(
        ts, emp["shift_end"], valid_date(record["date"]), emp["shift_start"]
    )
    update = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50,
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "Record was updated concurrently")
    record.update(update)
    return attendance_response(record)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from:
        valid_date(date_from)
    if date_to:
        valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    attendance_date = valid_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_date.isoformat()}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    changes = {}
    update = {}

    if body.status is not None:
        if body.status not in ALL_STATUSES:
            raise HTTPException(422, "Invalid status")
        if record.get("status") != body.status:
            changes["status"] = {
                "old": record.get("status"),
                "new": body.status,
            }
            update["status"] = body.status

    if body.punch_in is not None:
        new_punch_in = from_epoch_ms(body.punch_in)
        if record.get("punch_in") != new_punch_in:
            changes["punch_in"] = {
                "old": record.get("punch_in"),
                "new": new_punch_in,
            }
            update["punch_in"] = new_punch_in

    if body.punch_out is not None:
        new_punch_out = from_epoch_ms(body.punch_out)
        if record.get("punch_out") != new_punch_out:
            changes["punch_out"] = {
                "old": record.get("punch_out"),
                "new": new_punch_out,
            }
            update["punch_out"] = new_punch_out

    if not body.reason.strip():
        raise HTTPException(422, "Reason must not be empty")

    if update:
        punch_in_time = update.get("punch_in", record.get("punch_in"))
        punch_out_time = update.get("punch_out", record.get("punch_out"))

        if punch_in_time is not None and punch_out_time is not None:
            if punch_out_time <= punch_in_time:
                raise HTTPException(422, "Punch-out must be after punch-in")
            elapsed = (punch_out_time - punch_in_time).total_seconds()
            if elapsed > 24 * 3600:
                raise HTTPException(422, "Punch-out must be within 24 hours")

            hours = compute_work_hours(punch_in_time, punch_out_time)
            update["work_hours"] = hours
            update["half_day"] = hours < 4.50
            update["overtime_minutes"] = compute_overtime(
                punch_out_time,
                employee["shift_end"],
                attendance_date,
                employee["shift_start"],
            )
        else:
            update["work_hours"] = None
            update["half_day"] = False
            update["overtime_minutes"] = 0

        if "punch_in" in update:
            update["late_minutes"] = compute_late_minutes(
                punch_in_time, employee["shift_start"]
            )

        history_entry = {
            "at": whole_second(datetime.now(UTC)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        result = db.attendance_logs.update_one(
            {"_id": record["_id"]},
            {
                "$set": update,
                "$push": {"history": history_entry},
            },
        )
        if result.modified_count != 1:
            raise HTTPException(409, "Record was updated concurrently")

        record.update(update)
        record.setdefault("history", []).append(history_entry)

    return attendance_response(record)

