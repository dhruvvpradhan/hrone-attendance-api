# Employee Attendance & Analytics API

This project is a backend API for managing employee attendance and generating useful attendance reports. It is built with **Python, FastAPI and MongoDB**.

The main focus of the project is to keep attendance rules inside the backend so that important values such as late minutes, work hours and overtime are calculated consistently instead of being trusted from the client.

## What the API does

The application supports:

- Creating and listing employees
- Punching employees in and out
- Calculating late arrival, work hours and overtime
- Identifying half-day attendance
- Viewing attendance records with filters and pagination
- Correcting attendance records through regularization
- Maintaining an audit history for manual corrections
- Generating monthly employee summaries
- Generating department-level attendance summaries
- Showing employees with the highest late minutes
- Generating department attendance trends
- Checking MongoDB query execution using explain endpoints

## Technology Used

- **Python**
- **FastAPI** for the REST API
- **PyMongo** for MongoDB access
- **Pydantic** for request validation
- **MongoDB** for employee, attendance and history data
- **Uvicorn** for running the application

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── DECISIONS.md
├── REVIEW.md
├── DATA_MODEL.md
├── PROBLEM_STATEMENT.docx
├── openapi.yaml
├── requirements.txt
├── sample_seed.py
└── README.md
```

The main API implementation is kept in `app/main.py` as required by the assignment.

## Running the Project

Create a virtual environment first:

```bash
python -m venv .venv
```

Activate it on Windows:

```bash
.venv\Scripts\activate
```

Install the required packages:

```bash
pip install -r requirements.txt
```

Create a local `.env` file:

```text
MONGO_URI=your_mongodb_connection_string
MONGO_DB=hrone_attendance
```

The `.env` file is only for local configuration and is not included in the repository.

## Loading Sample Data

The project contains sample employee and attendance documents. They can be loaded using:

```bash
python sample_seed.py
```

MongoDB 6.0 or newer is expected.

## Starting the API

Run:

```bash
uvicorn app.main:app --port 8000
```

The API will then be available at:

```text
http://127.0.0.1:8000
```

FastAPI's interactive Swagger documentation can be opened at:

```text
http://127.0.0.1:8000/docs
```

## Attendance Handling

The API treats **IST as the business timezone** for determining the attendance date. Timestamps are stored as BSON datetime values in MongoDB.

The backend derives attendance-related values from the punch timestamps, including:

- Late minutes
- Work hours
- Overtime
- Half-day status

This avoids allowing the client to directly control calculated attendance values.

## Regularization

Attendance can be corrected using the regularization endpoint.

A regularization request contains a reason and the person making the change. The previous state and the changes are stored in the attendance record's history so that manual corrections can be traced later.

## Analytics

The analytics endpoints use MongoDB aggregation pipelines for operations such as:

- Monthly employee attendance
- Department summaries
- Late-arrival ranking
- Daily department trends
- Seven-day moving averages

The department calculations also consider employees who had already joined during the selected period, rather than depending only on employees who happen to have attendance records.

## MongoDB Indexes

The application creates indexes during startup for frequently used queries.

The `(emp_code, date)` attendance index is unique so that an employee cannot have multiple attendance records for the same attendance date.

Other indexes support employee lookups, date-based attendance queries and analytics-related filtering.

## Notes

The repository does not contain database credentials, `.env` files or other secrets.

The API was developed around the provided API contract and data model, with the main business rules implemented in the backend.