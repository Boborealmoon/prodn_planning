# Local Production Planner

A local, browser-based production planning application that replaces Excel/VBA production schedulers.
Uses Python (Flask) + SQLite. No cloud, no EXE, no heavy dependencies.

---

## Folder Structure

```
production_planner/
├── app.py                  # Main Flask application + scheduling engine
├── scheduler_app/schema.sql # SQLite database schema used by the app
├── seed_demo.sql           # Demo/mock data
├── requirements.txt        # Python dependencies (just Flask)
├── planner.db              # SQLite database (created on first run)
│
├── scheduler_app/templates/ # Active HTML page templates
│   ├── base.html           # Shared layout / sidebar
│   ├── process_sheets.html # Main working page
│   ├── parts_flows.html    # Routing master data
│   ├── machines.html       # Machine management
│   ├── calendar.html       # Working calendar
│   ├── materials.html      # Material tracking
│   ├── staffing.html       # Staff assignments
│   ├── summary.html        # Production summary
│   ├── gantt.html          # Gantt chart (machines × time)
│   └── history.html        # Archived blocks
│
├── static/
│   ├── css/main.css        # All styles
│   └── js/utils.js         # Shared JS helpers
│
├── INSTALL_DEPS.bat        # Install Python dependencies
├── RUN_APP.bat             # Start the application
├── RESET_DEMO_DB.bat       # Wipe and restore demo data
├── ENVIRONMENT_NOTES.txt   # Setup and troubleshooting
└── README.md               # This file
```

---

## Quick Start

### Step 1 — Install dependencies (first time only)
Double-click **INSTALL_DEPS.bat**

### Step 2 — Run the app
Double-click **RUN_APP.bat**

Your browser will open at **http://localhost:5000**

### Step 3 — Reset to demo data (optional)
Double-click **RESET_DEMO_DB.bat** and type YES when prompted.

---

## How to Use — Page by Page

### 📋 Process Sheets (Main Page)
This is the **primary working page** of the app.

- By default shows all **uncompleted** process sheets
- Toggle **Show Completed** to see finished jobs
- Use **search** and **filters** to narrow the list
- Click **▶** to expand a process sheet and see:
  - Editable header fields (due date, qty, status)
  - Selected flow and route label (e.g. `TN10, TM20, GR30`)
  - Operation steps with readiness status (Not Ready / Ready / Planned / Completed)
  - Planning blocks with row segments, actual output entry
  - Per-block actions: Lock, Split, Archive, Delete
- Click **⚡** next to any PS to auto-plan its next eligible operation
- Click **⚡ Auto Plan Active PS** in the toolbar to plan all active sheets at once
- Click **＋ Add Process Sheet** to open the creation modal

**Add Process Sheet modal:**
1. Enter PS ID, select Part
2. System loads valid flows for that part (auto-selects if only one)
3. Fill in quantities, dates, material info
4. Click Create

### 📊 Gantt
- Machines as **columns**, dates as **rows** (scrollable)
- Working windows (08:30–12:00, 12:45–16:00, 16:15–20:00) are highlighted in blue outlines
- Break periods and off-hours shown in grey hatching
- **Click** any block to open detail panel (lock/unlock, move, delete, archive)
- **Drag** blocks to move them (drop onto another date/machine column)
- Use the date navigation buttons to shift the view
- 24HR machines show blocks that can span midnight

### 📈 Summary
- Roll-up table of all process sheets with quantities, dates, machines used
- Key statistics: total PS, unplanned, planned, completed, overdue

### 🔩 Parts & Flows
- Left panel: list of parts — click to select
- Right panel: flow editor for the selected part
  - View/switch between multiple flows per part
  - Edit flow steps inline (drag to reorder, add/remove steps)
  - Each step: Op No, Op Type, Machine Category, Cycle Time, Setup Time, Preferred Machine
  - Exactly one step must be marked as "Last Op"
  - Click **💾 Save Flow** to save changes

### 🏭 Machines
- Add, edit, deactivate machines
- Set shift profile: **STANDARD** (08:30–20:00 working windows) or **24HR** (continuous run once started)
- Deactivated machines are excluded from auto-planning

### 📅 Calendar
- Click any day cell to toggle between Working / Off
- Use **Bulk Update** to mark a date range (e.g. public holidays)
- The scheduler reads this calendar — off days are skipped

### 📦 Materials
- Shows material status per process sheet
- Edit material readiness, ready quantity, need-by date, order status
- Add **Order Logs** (PO details, received quantities)
- **Covered Qty** = ready_qty + sum of received order logs
- Shortage warning shown when covered qty < total qty
- Materials are **warnings only** — they do not block planning

### 👷 Staffing
- Add staff members (Machinist / Operator)
- Assign staff to machines by date and shift
- Missing machinist warnings appear on Process Sheets page
- Staffing is **warnings only** — does not block planning

### 🗂️ History
- Shows archived planning blocks (moved via "Archive" action)
- Filter by PS ID
- History feeds downstream operation eligibility and completion logic

---

## Scheduling Engine — How It Works

### Auto-planning rules
1. All uncompleted process sheets are automatically in planning scope
2. Only the **next eligible operation** is planned per run (not all ops at once)
3. "Next eligible" = first op in sequence whose predecessor is ≥95% completed

### Duration calculation
- With setup charged: `total runtime = setup_time + (qty × cycle_time)`
- Without setup: `total runtime = qty × cycle_time`

### Standard machine (STANDARD shift)
- Working windows: 08:30–12:00, 12:45–16:00, 16:15–20:00
- **630 productive minutes per day**
- Break periods are not counted as productive time
- If qty doesn't fit in one day, it splits across multiple planning rows in the same block

### 24HR machines
- A **new block** must start during working hours (08:30–20:00)
- Once started, a block **may continue overnight**
- The scheduler enforces this on auto-plan and move operations

### Preferred machine
- Scheduler tries preferred machine first
- Falls back to other active machines in the same category

### Continuity
- Multiple planning rows in the same block = one logical continuous run
- Inserting another job between segments triggers a block split
- Locked blocks survive auto-replan

---

## Demo Data Included

The demo database includes:
- **4 parts**: SHAFT-A100, BRACKET-B200, HOUSING-C300, PLATE-D400
- **Multiple flows** including a 2-flow example on SHAFT-A100
- **8 machines**: 2 lathes, 2 mills, 1 grinder, 1 boring, 1 polisher, 1 24HR heat treatment furnace
- **5 process sheets** in various states
- **5 staff members**
- **Material records** with one shortage scenario and one ordered scenario
- **Calendar** pre-populated for ±90 days from today

---

## Known Limitations (V1)
