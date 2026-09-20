"""
Test script: draw a timeline (Gantt-style) chart of the tasks each employee
is currently handling - combines active leads (Lead-to-Sale) and open
support tickets, using each task's SLA due date as its end time.

Run:
    venv\\Scripts\\python.exe -m erp.task_timeline_chart

Output:
    task_timeline.png (open it to view)
"""
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from sqlalchemy import text

from erp.db import engine

QUERY = """
SELECT
    'Lead' AS task_type,
    l.id AS task_id,
    l.full_name AS task_name,
    rep.full_name AS employee,
    la.assigned_at AS start_time,
    COALESCE(l.sla_due_at, la.assigned_at + INTERVAL '1 day') AS end_time,
    COALESCE(l.sla_due_at IS NOT NULL AND now() > l.sla_due_at, FALSE) AS sla_breached
FROM leads l
JOIN lead_assignments la ON la.lead_id = l.id AND la.is_current
JOIN users rep ON rep.id = la.sales_rep_id

UNION ALL

SELECT
    'Ticket' AS task_type,
    t.id AS task_id,
    t.ticket_code AS task_name,
    ag.full_name AS employee,
    t.created_at AS start_time,
    COALESCE(t.sla_due_at, t.created_at + INTERVAL '1 day') AS end_time,
    COALESCE(t.sla_due_at IS NOT NULL AND now() > t.sla_due_at, FALSE) AS sla_breached
FROM tickets t
JOIN users ag ON ag.id = t.assignee_id
WHERE t.status NOT IN ('resolved', 'closed')

ORDER BY employee, start_time;
"""


def load_tasks() -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(text(QUERY), conn)
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["end_time"] = pd.to_datetime(df["end_time"])
    return df


def build_chart(df: pd.DataFrame, output_path: str = "task_timeline.png") -> None:
    if df.empty:
        print("No employee currently has an active task assigned.")
        return

    employees = list(df["employee"].unique())
    y_pos = {emp: i for i, emp in enumerate(employees)}

    fig, ax = plt.subplots(figsize=(10, 0.6 * len(df) + 2))

    for _, row in df.iterrows():
        start = mdates.date2num(row["start_time"])
        end = mdates.date2num(row["end_time"])
        color = "#c0392b" if row["sla_breached"] else "#2f8f7a"
        y = y_pos[row["employee"]]
        ax.barh(y, end - start, left=start, height=0.4, color=color)
        ax.text(start, y + 0.25, str(row["task_name"]), fontsize=8, va="bottom")

    ax.set_yticks(list(y_pos.values()))
    ax.set_yticklabels(list(y_pos.keys()))
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    fig.autofmt_xdate()
    ax.set_xlabel("Time")
    ax.set_ylabel("Employee")
    ax.set_title("Tasks currently handled per employee (green = on time, red = SLA breached)")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    print(f"Chart saved to: {output_path}")


if __name__ == "__main__":
    tasks_df = load_tasks()
    print(f"Found {len(tasks_df)} active task(s) across all employees.")
    if not tasks_df.empty:
        print(tasks_df[["employee", "task_type", "task_name", "sla_breached"]].to_string(index=False))
    build_chart(tasks_df)
