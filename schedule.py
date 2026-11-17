"""Schedules in the portable subset of systemd calendar syntax that the UI's quick builder produces.

    *-*-* *:00/15:00             every 15 minutes          {"kind": "minutes", "every": 15}
    *-*-* 00/6:30:00             every 6 hours at :30      {"kind": "hours", "every": 6, "minute": 30}
    *-*-* 02:00:00               daily at 02:00            {"kind": "daily", "hour": 2, "minute": 0}
    Mon..Wed,Fri *-*-* 02:00:00  weekly on those days      {"kind": "weekly", "days": [0, 1, 2, 4], ...}

On Linux any systemd expression works (systemd-analyze validates it). On Windows only this
subset is accepted, and it maps onto Task Scheduler calendar triggers.
"""
import datetime as dt
import re
from xml.sax.saxutils import escape

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WIN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_MIN = re.compile(r"^\*-\*-\* \*:0?0/(\d{1,2})(?::00)?$")
_HRS = re.compile(r"^\*-\*-\* 0?0/(\d{1,2}):(\d{1,2})(?::00)?$")
_AT = re.compile(r"^(?:([A-Za-z.,]+) )?\*-\*-\* (\d{1,2}):(\d{1,2})(?::00)?$")
UNSUPPORTED = ("Use the quick builder on Windows: every N minutes (dividing 60), every N hours (dividing 24), "
               "daily, or weekly on chosen days.")


def _days(spec):
    out = set()
    for part in spec.split(","):
        if ".." in part:
            a, b = part.split("..", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            if j < i:
                return None
            out.update(range(i, j + 1))
        elif part in DAYS:
            out.add(DAYS.index(part))
        else:
            return None
    return sorted(out)


def parse(expr):
    """Parse a portable expression into a dict, or return None."""
    e = " ".join(str(expr).split())
    m = _MIN.match(e)
    if m:
        n = int(m.group(1))
        return {"kind": "minutes", "every": n} if 1 <= n <= 30 and 60 % n == 0 else None
    m = _HRS.match(e)
    if m:
        n, mi = int(m.group(1)), int(m.group(2))
        return {"kind": "hours", "every": n, "minute": mi} if 1 <= n <= 12 and 24 % n == 0 and mi < 60 else None
    m = _AT.match(e)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        if h > 23 or mi > 59:
            return None
        if not m.group(1):
            return {"kind": "daily", "hour": h, "minute": mi}
        days = _days(m.group(1))
        if not days:
            return None
        return {"kind": "daily", "hour": h, "minute": mi} if len(days) == 7 else {"kind": "weekly", "days": days, "hour": h, "minute": mi}
    return None


def normalize(s):
    if s["kind"] == "minutes":
        return f"*-*-* *:00/{s['every']}:00"
    if s["kind"] == "hours":
        return f"*-*-* 00/{s['every']}:{s['minute']:02d}:00"
    t = f"{s['hour']:02d}:{s['minute']:02d}:00"
    if s["kind"] == "daily":
        return f"*-*-* {t}"
    return ",".join(DAYS[d] for d in s["days"]) + f" *-*-* {t}"


def matches(s, t):
    if s["kind"] == "minutes":
        return t.minute % s["every"] == 0
    if s["kind"] == "hours":
        return t.hour % s["every"] == 0 and t.minute == s["minute"]
    if (t.hour, t.minute) != (s["hour"], s["minute"]):
        return False
    return s["kind"] == "daily" or t.weekday() in s["days"]


def next_times(s, n=5, after=None):
    """The next n local datetimes the schedule fires, strictly after `after` (default now)."""
    t = (after or dt.datetime.now()).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    out, limit = [], t + dt.timedelta(days=8 * max(1, n))
    while len(out) < n and t < limit:
        if matches(s, t):
            out.append(t)
        t += dt.timedelta(minutes=1)
    return out


def preview(expr, n=5):
    s = parse(expr)
    if not s:
        raise ValueError(UNSUPPORTED)
    return {"normalized": normalize(s),
            "next": [{"text": t.strftime("%a %Y-%m-%d %H:%M:%S"), "epoch": t.timestamp()} for t in next_times(s, n)]}


# ------------------------------------------------------------------ Windows Task Scheduler
def win_trigger(s, delay_min=0, start=None):
    """A <CalendarTrigger> element for Task Scheduler XML, with children in the order Windows exports them."""
    day = (start or dt.date.today()).isoformat()
    delay = f"<RandomDelay>PT{int(delay_min)}M</RandomDelay>" if delay_min else ""
    rep = ""
    if s["kind"] in ("minutes", "hours"):
        interval = f"PT{s['every']}M" if s["kind"] == "minutes" else f"PT{s['every']}H"
        rep = (f"<Repetition><Interval>{interval}</Interval><Duration>P1D</Duration>"
               "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
        at = f"{day}T00:{0 if s['kind'] == 'minutes' else s['minute']:02d}:00"
    else:
        at = f"{day}T{s['hour']:02d}:{s['minute']:02d}:00"
    if s["kind"] == "weekly":
        days = "".join(f"<{WIN_DAYS[d]} />" for d in s["days"])
        sched = f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek>{days}</DaysOfWeek></ScheduleByWeek>"
    else:
        sched = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    return f"<CalendarTrigger>{rep}<StartBoundary>{at}</StartBoundary><Enabled>true</Enabled>{delay}{sched}</CalendarTrigger>"


def win_task_xml(description, triggers, command, arguments, workdir, enabled=True, catch_up=True,
                 user_id="S-1-5-18", interactive=False, restart=False, priority=7):
    """A complete Task Scheduler 1.2 task definition. Default principal: SYSTEM (S-1-5-18)."""
    principal = (f"<UserId>{escape(user_id)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>"
                 if interactive else f"<UserId>{escape(user_id)}</UserId><RunLevel>HighestAvailable</RunLevel>")
    restart_xml = "<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>" if restart else ""
    b = lambda v: "true" if v else "false"  # noqa: E731
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>Restic Console</Author><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers>{"".join(triggers)}</Triggers>
  <Principals><Principal id="Author">{principal}</Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>{b(catch_up)}</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>{b(enabled)}</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>{int(priority)}</Priority>{restart_xml}
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments>
    <WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec></Actions>
</Task>
"""
