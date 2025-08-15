"""Generate the Tableau workbook (Uber_NCR_Dashboard.twb + packaged .twbx).

Every chart is filtered by one boolean calc driven by global parameters
(Month, Vehicle Type, Time Slot, Pickup Location), so the dropdowns on the
dashboard change every chart at once. A "Metric" parameter swaps the measure
shown in the trend, vehicle, heatmap and top-pickup charts.

Run from the project root after clean_data.py:  python scripts/build_workbook.py
"""
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pandas as pd
from tableauhyperapi import (Connection, CreateMode, HyperProcess, Inserter, SqlType,
                             TableDefinition, TableName, Telemetry)

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "clean" / "uber_rides_clean.csv"
HYPER = ROOT / "data" / "extract" / "uber_rides.hyper"
TWB = ROOT / "tableau" / "Uber_NCR_Dashboard.twb"
TWBX = ROOT / "tableau" / "Uber_NCR_Dashboard.twbx"

DS = "federated.uber01"
DS_CAPTION = "Uber NCR Rides"
W, H = 1400, 900  # dashboard size in px

# Presentation theme: navy stage, dark cards, one bright accent per chart.
NAVY, CARD, LINE = "#0A0F24", "#121A3B", "#2A3566"
FG, MUTED, INK = "#F5F7FF", "#AEB6DD", "#0A0F24"
TEAL, CYAN, VIOLET, PINK = "#2DD4BF", "#38BDF8", "#A78BFA", "#F472B6"
CORAL, ORANGE, AMBER, GREEN, BLUE = "#FB7185", "#FB923C", "#FBBF24", "#34D399", "#60A5FA"
CTRL_BG = TEAL  # one colour for every dropdown strip; dark labels stay readable on it
STATUS_COLORS = {
    "Completed": GREEN,
    "Cancelled by Driver": CORAL,
    "Cancelled by Customer": ORANGE,
    "No Driver Found": "#94A3B8",
    "Incomplete": AMBER,
}
VEHICLE_COLORS = {
    "Auto": CYAN, "Go Mini": VIOLET, "Go Sedan": PINK, "Bike": AMBER,
    "Premier Sedan": GREEN, "eBike": ORANGE, "Uber XL": BLUE,
}
PAYMENT_COLORS = {"UPI": CYAN, "Cash": GREEN, "Uber Wallet": VIOLET, "Credit Card": PINK, "Debit Card": AMBER}
BAND_COLORS = {"0-5 km": "#FDE047", "5-10 km": AMBER, "10-20 km": ORANGE, "20-35 km": PINK, "35-50 km": VIOLET}
# Continuous ramps (low -> high) for colour-by-value charts.
RAMP_HEAT = ["#3B5BDB", "#2563EB", "#7C3AED", "#DB2777", "#F59E0B"]
RAMP_TEAL = ["#155E75", TEAL, CYAN, VIOLET]
RAMP_PINK = ["#7C2D5B", PINK, ORANGE]
KPI_COLORS_1 = [TEAL, GREEN, VIOLET, AMBER, PINK, BLUE]
KPI_COLORS_2 = [CORAL, "#F87171", ORANGE, "#94A3B8", "#22D3EE", "#FDE047"]


def q(value: str) -> str:
    """Quoted, escaped XML attribute value."""
    return quoteattr(value)


def tstr(value: str) -> str:
    """Tableau string literal, e.g. "Auto"."""
    return '"' + value.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Source columns
# --------------------------------------------------------------------------
df = pd.read_csv(CSV)
DTYPES = {"int64": "integer", "float64": "real"}
DIM_INTS = {"Ride Key", "Month Num", "Weekday Num", "Hour"}


def tableau_type(col: str) -> str:
    if col == "Date":
        return "date"
    if col == "Booking Datetime":
        return "datetime"
    return DTYPES.get(str(df[col].dtype), "string")


SOURCE = {}
for c in df.columns:
    dt = tableau_type(c)
    if dt in ("integer", "real") and c not in DIM_INTS:
        SOURCE[c] = (dt, "measure", "quantitative")
    elif c in DIM_INTS:
        SOURCE[c] = (dt, "dimension", "ordinal")
    else:
        SOURCE[c] = (dt, "dimension", "nominal")


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------
METRICS = ["Bookings", "Completed Rides", "Revenue (INR)", "Avg Fare (INR)", "Completion Rate %"]
PARAMS = {
    "Parameter 1": ("Metric", METRICS),
    "Parameter 2": ("Month", ["All"] + sorted(df["Month"].unique())),
    "Parameter 3": ("Vehicle Type", ["All"] + sorted(df["Vehicle Type"].unique())),
    "Parameter 4": ("Time Slot", ["All"] + sorted(df["Time Slot"].unique())),
    "Parameter 5": ("Pickup Location", ["All"] + sorted(df["Pickup Location"].unique())),
}


def param_xml(name: str) -> str:
    caption, members = PARAMS[name]
    first = tstr(members[0])
    mem = "".join(f"<member value={q(tstr(m))} />" for m in members)
    return (
        f"<column caption={q(caption)} datatype='string' name='[{name}]' "
        f"param-domain-type='list' role='measure' type='nominal' value={q(first)}>"
        f"<calculation class='tableau' formula={q(first)} />"
        f"<members>{mem}</members></column>"
    )


def pref(name: str) -> str:
    return f"[Parameters].[{name}]"


# --------------------------------------------------------------------------
# Calculated fields: id -> (caption, datatype, role, type, formula, format, source deps)
# --------------------------------------------------------------------------
def all_or(param: str, field: str) -> str:
    return f'({pref(param)} = "All" OR [{field}] = {pref(param)})'


CALCS = {
    "Calculation_filter": (
        "Dashboard Filter", "boolean", "dimension", "nominal",
        " AND ".join([
            all_or("Parameter 2", "Month"),
            all_or("Parameter 3", "Vehicle Type"),
            all_or("Parameter 4", "Time Slot"),
            all_or("Parameter 5", "Pickup Location"),
        ]),
        None, ["Month", "Vehicle Type", "Time Slot", "Pickup Location"],
    ),
    "Calculation_metric": (
        "Selected Metric", "real", "measure", "quantitative",
        f"CASE {pref('Parameter 1')}\n"
        '  WHEN "Bookings" THEN SUM([Ride Count])\n'
        '  WHEN "Completed Rides" THEN SUM([Is Completed])\n'
        '  WHEN "Revenue (INR)" THEN SUM([Revenue])\n'
        '  WHEN "Avg Fare (INR)" THEN SUM([Revenue]) / SUM([Is Completed])\n'
        '  WHEN "Completion Rate %" THEN 100 * SUM([Is Completed]) / SUM([Ride Count])\n'
        "END",
        "n#,##0", ["Ride Count", "Is Completed", "Revenue"],
    ),
    "Calculation_compl": (
        "Completion Rate", "real", "measure", "quantitative",
        "SUM([Is Completed]) / SUM([Ride Count])", "p0.0%", ["Is Completed", "Ride Count"],
    ),
    "Calculation_revm": (
        "Revenue (INR M)", "real", "measure", "quantitative",
        "SUM([Revenue]) / 1000000", '*"₹"#,##0.00" M"', ["Revenue"],
    ),
    "Calculation_fare": (
        "Avg Fare (INR)", "real", "measure", "quantitative",
        "SUM([Revenue]) / SUM([Is Completed])", '*"₹"#,##0', ["Revenue", "Is Completed"],
    ),
    "Calculation_cancel": (
        "Cancel Rate", "real", "measure", "quantitative",
        "(SUM([Is Cancelled Customer]) + SUM([Is Cancelled Driver])) / SUM([Ride Count])",
        "p0.0%", ["Is Cancelled Customer", "Is Cancelled Driver", "Ride Count"],
    ),
    "Calculation_dcancel": (
        "Driver Cancels", "real", "measure", "quantitative",
        "SUM([Is Cancelled Driver]) / SUM([Ride Count])", "p0.0%",
        ["Is Cancelled Driver", "Ride Count"],
    ),
    "Calculation_ccancel": (
        "Customer Cancels", "real", "measure", "quantitative",
        "SUM([Is Cancelled Customer]) / SUM([Ride Count])", "p0.0%",
        ["Is Cancelled Customer", "Ride Count"],
    ),
    "Calculation_nodriver": (
        "No Driver Found", "real", "measure", "quantitative",
        "SUM([Is No Driver]) / SUM([Ride Count])", "p0.0%", ["Is No Driver", "Ride Count"],
    ),
    "Calculation_hasfail": (
        "Has Failure Reason", "boolean", "dimension", "nominal",
        "NOT ISNULL([Failure Reason])", None, ["Failure Reason"],
    ),
    "Calculation_hasband": (
        "Has Distance Band", "boolean", "dimension", "nominal",
        "NOT ISNULL([Distance Band])", None, ["Distance Band"],
    ),
    "Calculation_paid": (
        "Is Paid", "boolean", "dimension", "nominal",
        '[Payment Method] <> "Not Paid"', None, ["Payment Method"],
    ),
}

# Formats applied to plain source measures.
SOURCE_FORMATS = {
    "Ride Count": "n#,##0",
    "Is Completed": "n#,##0",
    "Revenue": '*"₹"#,##0',
    "Ride Distance": "n#,##0.0",
    "Avg VTAT": "n#,##0.0",
    "Driver Ratings": "n0.00",
    "Customer Rating": "n0.00",
}


def column_xml(field: str) -> str:
    """<column> definition for a source field or calc, as used in dependencies."""
    if field in CALCS:
        caption, dt, role, typ, formula, fmt, _ = CALCS[field]
        fmt_attr = f" default-format={q(fmt)}" if fmt else ""
        return (
            f"<column caption={q(caption)} datatype='{dt}'{fmt_attr} name='[{field}]' "
            f"role='{role}' type='{typ}'>"
            f"<calculation class='tableau' formula={q(formula)} /></column>"
        )
    dt, role, typ = SOURCE[field]
    fmt = SOURCE_FORMATS.get(field)
    fmt_attr = f" default-format={q(fmt)}" if fmt else ""
    return f"<column datatype='{dt}'{fmt_attr} name='[{field}]' role='{role}' type='{typ}' />"


# --------------------------------------------------------------------------
# Field references used on shelves ("instances")
# --------------------------------------------------------------------------
class Inst:
    PREFIX = {"None": "none", "Sum": "sum", "Avg": "avg", "User": "usr"}

    def __init__(self, field: str, derivation: str = "None", kind: str = "nk"):
        self.field, self.derivation, self.kind = field, derivation, kind
        typ = {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}[kind]
        self.name = f"[{self.PREFIX[derivation]}:{field}:{kind}]"
        self.xml = (
            f"<column-instance column='[{field}]' derivation='{derivation}' "
            f"name='{self.name}' pivot='key' type='{typ}' />"
        )

    @property
    def ref(self) -> str:
        return f"[{DS}].{self.name}"


def dim(f, kind="nk"):
    return Inst(f, "None", kind)


def total(f):
    return Inst(f, "Sum", "qk")


def mean(f):
    return Inst(f, "Avg", "qk")


def calc(f):
    return Inst(f, "User", "qk")


FILTER = dim("Calculation_filter")
METRIC = calc("Calculation_metric")
RIDES = total("Ride Count")


# --------------------------------------------------------------------------
# Worksheets
# --------------------------------------------------------------------------
def member_filter(inst: Inst, member: str = "true") -> str:
    return (
        f"<filter class='categorical' column='{inst.ref}'>"
        f"<groupfilter function='member' level='{inst.name}' member={q(member)} "
        "user:ui-domain='database' user:ui-enumeration='inclusive' user:ui-marker='enumerate' />"
        "</filter>"
    )


def top_filter(inst: Inst, by_formula: str, n: int) -> str:
    return (
        f"<filter class='categorical' column='{inst.ref}'>"
        f"<groupfilter count='{n}' end='top' function='end' units='records' "
        "user:ui-marker='end' user:ui-top-by-field='true'>"
        f"<groupfilter direction='DESC' expression={q(by_formula)} function='order' user:ui-marker='order'>"
        f"<groupfilter function='level-members' level='{inst.name}' "
        "user:ui-enumeration='all' user:ui-marker='enumerate' />"
        "</groupfilter></groupfilter></filter>"
    )


def ramp_encoding(inst: Inst, colors) -> str:
    """Continuous colour encoding with a custom low->high palette."""
    cs = "".join(f"<color>{c}</color>" for c in colors)
    return (f"<encoding attr='color' field='{inst.ref}' type='custom-interpolated'>"
            f"<color-palette custom='true' name='' type='ordered-sequential'>{cs}</color-palette>"
            "</encoding>")


def worksheet(name, *, rows=None, cols=None, mark="Bar", color=None, text=None,
              labels=False, mark_color=None, sort=None, manual_sort=None,
              extra_filters=(), extra_slices=(), kpi=None, accent=CYAN, ramp=None,
              kpi_bg=TEAL, mark_border=None):
    """Build one <worksheet>. rows/cols/color/text are Inst objects."""
    insts = [i for i in (rows, cols, color, text, kpi, FILTER) if i is not None]
    insts += [i for i, _ in extra_filters]
    if sort:
        insts += [sort[0], sort[1]]
    seen, inst_xml, fields = set(), [], []
    for i in insts:
        if i.name not in seen:
            seen.add(i.name)
            inst_xml.append(i.xml)
        fields.append(i.field)
    # Calcs need their source columns declared too.
    for f in list(fields):
        if f in CALCS:
            fields += CALCS[f][6]
    cols_xml = "".join(column_xml(f) for f in dict.fromkeys(fields))
    params_xml = "".join(param_xml(p) for p in PARAMS)

    filters = member_filter(FILTER)
    slices = [FILTER.ref]
    for inst, fxml in extra_filters:
        filters += fxml
        slices.append(inst.ref)
    slices += list(extra_slices)
    sorts = ""
    if sort:
        sorts += (f"<sort class='computed' column='{sort[0].ref}' direction='{sort[2]}' "
                  f"using='{sort[1].ref}' />")
    if manual_sort:
        inst, order = manual_sort
        buckets = "".join(f"<bucket>{escape(tstr(v))}</bucket>" for v in order)
        sorts += (f"<sort class='manual' column='{inst.ref}' direction='ASC'>"
                  f"<dictionary>{buckets}</dictionary></sort>")

    enc = ""
    if color:
        enc += f"<color column='{color.ref}' />"
    if text:
        enc += f"<text column='{text.ref}' />"
    if kpi:
        enc += f"<text column='{kpi.ref}' />"
    mark_rules = ""
    if labels:
        mark_rules += "<format attr='mark-labels-show' value='true' />"
    if mark_color:
        mark_rules += f"<format attr='mark-color' value='{mark_color}' />"
    if mark_border:
        mark_rules += f"<format attr='stroke-color' value='{mark_border}' />"
    if kpi:
        # Bold title above a big centred number, all on a bright tile.
        mark = "Text"
        title_xml = (
            "<layout-options><title><formatted-text>"
            f"<run bold='true' fontcolor='{INK}' fontsize='10'>{escape(name)}</run>"
            "</formatted-text></title></layout-options>"
        )
        table_style = (
            "<style>"
            f"<style-rule element='table'><format attr='background-color' value='{kpi_bg}' /></style-rule>"
            f"<style-rule element='title'><format attr='background-color' value='{kpi_bg}' /></style-rule>"
            "<style-rule element='cell'>"
            f"<format attr='color' value='{INK}' /><format attr='font-size' value='24' />"
            "<format attr='font-weight' value='bold' /><format attr='text-align' value='center' />"
            "</style-rule></style>"
        )
    else:
        # Coloured square + white title on a dark card; light axis text, no gridlines.
        title_xml = (
            "<layout-options><title><formatted-text>"
            f"<run fontcolor='{accent}' fontsize='14'>■  </run>"
            f"<run bold='true' fontcolor='{FG}' fontsize='13'>{escape(name)}</run>"
            "</formatted-text></title></layout-options>"
        )
        table_style = (
            "<style>"
            f"<style-rule element='worksheet'><format attr='color' value='{MUTED}' /></style-rule>"
            f"<style-rule element='table'><format attr='background-color' value='{CARD}' /></style-rule>"
            f"<style-rule element='title'><format attr='background-color' value='{CARD}' /></style-rule>"
            f"<style-rule element='header'><format attr='color' value='{FG}' /></style-rule>"
            f"<style-rule element='label'><format attr='color' value='{FG}' /></style-rule>"
            f"<style-rule element='gridline'><format attr='line-visibility' value='off' /></style-rule>"
            + (f"<style-rule element='mark'>{ramp_encoding(color, ramp)}</style-rule>" if ramp and color else "")
            + (("<style-rule element='table-div'>"
                + "".join(f"<format attr='{a}' scope='{sc}' value='{v}' />"
                          for sc in ("rows", "cols")
                          for a, v in (("stroke-color", mark_border), ("stroke-size", "3"),
                                       ("line-visibility", "on"), ("div-level", "1")))
                + "</style-rule>") if mark_border else "")
            + "</style>"
        )
    pane_style = (f"<style><style-rule element='mark'>{mark_rules}</style-rule></style>"
                  if mark_rules else "")

    return f"""
    <worksheet name={q(name)}>
      {title_xml}
      <table>
        <view>
          <datasources>
            <datasource caption={q(DS_CAPTION)} name='{DS}' />
            <datasource name='Parameters' />
          </datasources>
          <datasource-dependencies datasource='{DS}'>{cols_xml}{''.join(inst_xml)}</datasource-dependencies>
          <datasource-dependencies datasource='Parameters'>{params_xml}</datasource-dependencies>
          {filters}
          {sorts}
          <slices>{''.join(f'<column>{s}</column>' for s in slices)}</slices>
          <aggregation value='true' />
        </view>
        {table_style}
        <panes>
          <pane selection-relaxation-option='selection-relaxation-allow'>
            <view><breakdown value='auto' /></view>
            <mark class='{mark}' />
            <encodings>{enc}</encodings>
            {pane_style}
          </pane>
        </panes>
        <rows>{rows.ref if rows else ''}</rows>
        <cols>{cols.ref if cols else ''}</cols>
      </table>
    </worksheet>"""


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FAIL = dim("Calculation_hasfail")
PAID = dim("Calculation_paid")
BAND_OK = dim("Calculation_hasband")
PICKUP = dim("Pickup Location")

KPI1 = ["Total Bookings", "Completed Rides", "Completion Rate", "Revenue", "Avg Fare",
        "Avg Distance km"]
KPI2 = ["Cancel Rate", "Driver Cancels", "Customer Cancels", "No Driver Found",
        "Pickup ETA (min)", "Avg Driver Rating"]
KPI_BG = {**dict(zip(KPI1, KPI_COLORS_1)), **dict(zip(KPI2, KPI_COLORS_2))}

SHEETS = {
    # ---- Dashboard 1 KPIs
    "Total Bookings": dict(kpi=RIDES),
    "Completed Rides": dict(kpi=total("Is Completed")),
    "Completion Rate": dict(kpi=calc("Calculation_compl")),
    "Revenue": dict(kpi=calc("Calculation_revm")),
    "Avg Fare": dict(kpi=calc("Calculation_fare")),
    "Avg Distance km": dict(kpi=mean("Ride Distance")),
    # ---- Dashboard 1 charts
    "Monthly Trend (Selected Metric)": dict(
        cols=dim("Month", "ok"), rows=METRIC, color=METRIC, ramp=RAMP_TEAL, labels=True, accent=TEAL),
    "Booking Status Mix": dict(
        rows=dim("Booking Status"), cols=RIDES, color=dim("Booking Status"), labels=True,
        sort=(dim("Booking Status"), RIDES, "DESC"), accent=GREEN),
    "Selected Metric by Vehicle Type": dict(
        rows=dim("Vehicle Type"), cols=METRIC, color=dim("Vehicle Type"), labels=True,
        sort=(dim("Vehicle Type"), METRIC, "DESC"), accent=BLUE),
    "Demand Heatmap (Weekday x Hour)": dict(
        rows=dim("Weekday"), cols=dim("Hour", "ok"), mark="Square", color=METRIC, ramp=RAMP_HEAT,
        manual_sort=(dim("Weekday"), WEEKDAYS), accent=VIOLET, mark_border=NAVY),
    "Top 10 Pickup Locations": dict(
        rows=PICKUP, cols=METRIC, color=METRIC, ramp=RAMP_PINK, labels=True,
        sort=(PICKUP, METRIC, "DESC"), accent=PINK,
        extra_filters=[(PICKUP, top_filter(PICKUP, "[Calculation_metric]", 10))]),
    # ---- Dashboard 2 KPIs
    "Cancel Rate": dict(kpi=calc("Calculation_cancel")),
    "Driver Cancels": dict(kpi=calc("Calculation_dcancel")),
    "Customer Cancels": dict(kpi=calc("Calculation_ccancel")),
    "No Driver Found": dict(kpi=calc("Calculation_nodriver")),
    "Pickup ETA (min)": dict(kpi=mean("Avg VTAT")),
    "Avg Driver Rating": dict(kpi=mean("Driver Ratings")),
    # ---- Dashboard 2 charts
    "Why Rides Fail": dict(
        rows=dim("Failure Reason"), cols=RIDES, color=dim("Booking Status"), labels=True,
        sort=(dim("Failure Reason"), RIDES, "DESC"), accent=CORAL,
        extra_filters=[(FAIL, member_filter(FAIL))]),
    "Booking Outcome by Time Slot": dict(
        rows=dim("Time Slot"), cols=RIDES, color=dim("Booking Status"), accent=AMBER),
    "Revenue by Payment Method": dict(
        rows=dim("Payment Method"), cols=total("Revenue"), color=dim("Payment Method"), labels=True,
        sort=(dim("Payment Method"), total("Revenue"), "DESC"), accent=CYAN,
        extra_filters=[(PAID, member_filter(PAID))]),
    "Avg Driver Rating by Vehicle": dict(
        rows=dim("Vehicle Type"), cols=mean("Driver Ratings"), labels=True, mark_color=AMBER,
        sort=(dim("Vehicle Type"), mean("Driver Ratings"), "DESC"), accent=GREEN),
    "Revenue by Distance Band": dict(
        cols=dim("Distance Band"), rows=calc("Calculation_revm"), color=dim("Distance Band"), labels=True,
        accent=ORANGE, extra_filters=[(BAND_OK, member_filter(BAND_OK))]),
}
for _k in KPI_BG:
    SHEETS[_k]["kpi_bg"] = KPI_BG[_k]


# --------------------------------------------------------------------------
# Dashboards (tiled layout, coordinates in 1/100000 of the dashboard)
# --------------------------------------------------------------------------
class Ids:
    n = 2

    @classmethod
    def next(cls):
        cls.n += 1
        return cls.n


def box(x, y, w, h):
    sx, sy = 100000 / W, 100000 / H
    return f"x='{round(x * sx)}' y='{round(y * sy)}' w='{round(w * sx)}' h='{round(h * sy)}'"


def row_zones(y, h, items, gap=0):
    """Split full width into zones; items = [(weight, zone_fn)]."""
    total_w = sum(wt for wt, _ in items)
    out, x = [], 0
    for i, (wt, fn) in enumerate(items):
        w = W - x if i == len(items) - 1 else round(W * wt / total_w)
        out.append(fn(x, y, w, h))
        x += w
    return "".join(out)


def sheet_zone(name, show_title=True):
    title = "" if show_title else " show-title='false'"
    bg = KPI_BG.get(name, CARD)
    border = bg if name in KPI_BG else LINE

    def fn(x, y, w, h):
        return (f"<zone {box(x, y, w, h)} id='{Ids.next()}' name={q(name)}{title}>"
                f"<zone-style><format attr='background-color' value='{bg}' />"
                f"<format attr='border-color' value='{border}' />"
                "<format attr='border-style' value='solid' /><format attr='border-width' value='1' />"
                "<format attr='margin' value='5' /><format attr='padding' value='4' /></zone-style></zone>")
    return fn


def param_zone(pname):
    def fn(x, y, w, h):
        return (f"<zone {box(x, y, w, h)} id='{Ids.next()}' mode='compact' "
                f"param='{pref(pname)}' type-v2='paramctrl'>"
                f"<zone-style><format attr='background-color' value='{CTRL_BG}' />"
                "<format attr='margin' value='5' /><format attr='padding' value='4' /></zone-style></zone>")
    return fn


def text_zone(x, y, w, h, title, subtitle):
    head, _, rest = title.partition(" - ")
    return (
        f"<zone {box(x, y, w, h)} id='{Ids.next()}' type-v2='text'>"
        "<formatted-text>"
        f"<run bold='true' fontcolor='{FG}' fontsize='20'>{escape(head)}</run>"
        f"<run bold='true' fontcolor='{TEAL}' fontsize='20'>{escape('  ' + rest if rest else '')}</run>"
        "<run>Æ&#10;</run>"
        f"<run fontcolor='{MUTED}' fontsize='11'>{escape(subtitle)}</run>"
        "</formatted-text>"
        f"<zone-style><format attr='background-color' value='{NAVY}' />"
        "<format attr='padding' value='12' /></zone-style></zone>"
    )


def dashboard(name, subtitle, kpis, row1, row2):
    header_h, ctrl_h, kpi_h = 86, 54, 100
    chart_h = (H - header_h - ctrl_h - kpi_h) // 2
    y1 = header_h + ctrl_h + kpi_h
    zones = (
        text_zone(0, 0, W, header_h, name, subtitle)
        + row_zones(header_h, ctrl_h, [(1, param_zone(p)) for p in PARAMS])
        + row_zones(header_h + ctrl_h, kpi_h, [(1, sheet_zone(k)) for k in kpis])
        + row_zones(y1, chart_h, [(wt, sheet_zone(s)) for wt, s in row1])
        + row_zones(y1 + chart_h, H - y1 - chart_h, [(wt, sheet_zone(s)) for wt, s in row2])
    )
    return f"""
    <dashboard name={q(name)}>
      <style><style-rule element='table'><format attr='background-color' value='{NAVY}' /></style-rule><style-rule element='parameter-ctrl-title'><format attr='font-size' value='14' /><format attr='font-weight' value='bold' /><format attr='color' value='{INK}' /></style-rule><style-rule element='parameter-ctrl'><format attr='font-size' value='14' /><format attr='color' value='{INK}' /></style-rule></style>
      <size sizing-mode='automatic' />
      <datasources>
        <datasource caption={q(DS_CAPTION)} name='{DS}' />
        <datasource name='Parameters' />
      </datasources>
      <zones>
        <zone h='100000' id='{Ids.next()}' type-v2='layout-basic' w='100000' x='0' y='0'>{zones}<zone-style><format attr='background-color' value='{NAVY}' /></zone-style></zone>
      </zones>
    </dashboard>"""


D1 = "Uber NCR 2024 - Ride Overview"
D2 = "Uber NCR 2024 - Cancellations & Ops"
SUB = "150,000 bookings across Delhi-NCR | Use the dropdowns to filter every chart | Click a bar to cross-filter"

DASHBOARDS = [
    (D1, dashboard(
        D1, SUB,
        KPI1,
        [(60, "Monthly Trend (Selected Metric)"), (40, "Booking Status Mix")],
        [(30, "Selected Metric by Vehicle Type"), (40, "Demand Heatmap (Weekday x Hour)"),
         (30, "Top 10 Pickup Locations")],
    ), ["Booking Status Mix", "Selected Metric by Vehicle Type", "Monthly Trend (Selected Metric)"]),
    (D2, dashboard(
        D2, SUB,
        KPI2,
        [(55, "Why Rides Fail"), (45, "Booking Outcome by Time Slot")],
        [(33, "Revenue by Payment Method"), (34, "Avg Driver Rating by Vehicle"),
         (33, "Revenue by Distance Band")],
    ), ["Avg Driver Rating by Vehicle", "Revenue by Payment Method"]),
]


def filter_actions() -> str:
    out = []
    for dash, _, sources in DASHBOARDS:
        for src in sources:
            out.append(
                f"<action caption={q('Filter from ' + src)} name='[Action_{uuid.uuid4().hex[:8]}]'>"
                "<activation auto-clear='true' type='on-select' />"
                f"<source dashboard={q(dash)} type='sheet' worksheet={q(src)} />"
                "<command command='tsc:tsl-filter'>"
                "<param name='special-fields' value='all' />"
                f"<param name='target' value={q(dash)} />"
                "</command></action>"
            )
    return f"<actions>{''.join(out)}</actions>"


# --------------------------------------------------------------------------
# Data source
# --------------------------------------------------------------------------
HYPER_TYPES = {"integer": SqlType.big_int(), "real": SqlType.double(),
               "string": SqlType.text(), "date": SqlType.date(),
               "datetime": SqlType.timestamp()}


def build_extract() -> None:
    """Write the CSV into a .hyper extract (Tableau Public refuses live CSV connections)."""
    HYPER.parent.mkdir(parents=True, exist_ok=True)
    table = TableDefinition(TableName("Extract", "Extract"), [
        TableDefinition.Column(c, HYPER_TYPES[SOURCE[c][0]]) for c in df.columns
    ])
    rows = df.astype(object).where(df.notna(), None)
    rows["Date"] = pd.to_datetime(df["Date"]).dt.date
    rows["Booking Datetime"] = pd.to_datetime(df["Booking Datetime"]).dt.to_pydatetime()
    with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, str(HYPER), CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema("Extract")
            conn.catalog.create_table(table)
            with Inserter(conn, table) as ins:
                ins.add_rows(rows.itertuples(index=False, name=None))
                ins.execute()


def extract_xml(dbname: str) -> str:
    stamp = datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")
    return (
        "<extract count='-1' enabled='true' units='records'>"
        f"<connection access_mode='readonly' author-locale='en_US' class='hyper' dbname={q(dbname)} "
        "default-settings='yes' schema='Extract' sslmode='' tablename='Extract' "
        f"update-time={q(stamp)} username='tableau_internal_user'>"
        "<relation name='Extract' table='[Extract].[Extract]' type='table' />"
        "</connection></extract>"
    )


def datasource(directory: str, hyper_path: str) -> str:
    rel_cols = "".join(
        f"<column datatype='{SOURCE[c][0]}' name={q(c)} ordinal='{i}' />"
        for i, c in enumerate(df.columns)
    )
    ds_cols = "".join(column_xml(c) for c in SOURCE)
    calc_cols = "".join(column_xml(c) for c in CALCS)
    def cmap(colors):
        return "".join(f"<map to='{hexv}'><bucket>{escape(tstr(k))}</bucket></map>"
                       for k, hexv in colors.items())
    palettes = [("Booking Status", STATUS_COLORS), ("Vehicle Type", VEHICLE_COLORS),
                ("Payment Method", PAYMENT_COLORS), ("Distance Band", BAND_COLORS)]
    pal_insts = "".join(dim(f).xml for f, _ in palettes)
    pal_rules = "".join(
        f"<encoding attr='color' field='{dim(f).name}' type='palette'>{cmap(c)}</encoding>"
        for f, c in palettes)
    return f"""
    <datasource caption={q(DS_CAPTION)} inline='true' name='{DS}' version='18.1'>
      <connection class='federated'>
        <named-connections>
          <named-connection caption='uber_rides_clean' name='textscan.uber01'>
            <connection class='textscan' directory={q(directory)} filename='uber_rides_clean.csv' password='' server='' />
          </named-connection>
        </named-connections>
        <relation connection='textscan.uber01' name='uber_rides_clean.csv' table='[uber_rides_clean#csv]' type='table'>
          <columns character-set='UTF-8' header='yes' locale='en_IN' separator=','>{rel_cols}</columns>
        </relation>
      </connection>
      <aliases enabled='yes' />
      {ds_cols}
      {calc_cols}
      {pal_insts}
      {extract_xml(hyper_path)}
      <layout dim-ordering='alphabetic' dim-percentage='0.5' measure-ordering='alphabetic' measure-percentage='0.4' show-structure='true' />
      <style>
        <style-rule element='mark'>{pal_rules}</style-rule>
      </style>
    </datasource>"""


def workbook(directory: str, hyper_path: str) -> str:
    params = "".join(param_xml(p) for p in PARAMS)
    sheets = "".join(worksheet(n, **kw) for n, kw in SHEETS.items())
    dashes = "".join(d for _, d, _ in DASHBOARDS)
    # Every sheet on a dashboard is sized to "Entire View" so nothing scrolls.
    fit_all = "".join(
        f"<viewpoint name={q(s)}><zoom type='entire-view' /></viewpoint>" for s in SHEETS
    )
    windows = "".join(
        f"<window class='dashboard' maximized={q('true' if i == 0 else 'false')} name={q(n)}>"
        f"<viewpoints>{fit_all}</viewpoints><active id='-1' /></window>"
        for i, (n, _, _) in enumerate(DASHBOARDS)
    ) + "".join(
        f"<window class='worksheet' hidden='true' name={q(n)}>"
        "<cards><edge name='left'><strip size='160'><card type='pages' /><card type='filters' />"
        "<card type='marks' /></strip></edge></cards></window>"
        for n in SHEETS
    )
    return f"""<?xml version='1.0' encoding='utf-8' ?>
<workbook original-version='18.1' source-build='2024.3.0 (20243.24.1010.1014)' source-platform='win' version='18.1' xmlns:user='http://www.tableausoftware.com/xml/user'>
  <preferences>
    <preference name='ui.encoding.shelf.height' value='24' />
    <preference name='ui.shelf.height' value='26' />
  </preferences>
  <datasources>
    <datasource hasconnection='false' inline='true' name='Parameters' version='18.1'>
      <aliases enabled='yes' />
      {params}
    </datasource>
    {datasource(directory, hyper_path)}
  </datasources>
  {filter_actions()}
  <worksheets>{sheets}
  </worksheets>
  <dashboards>{dashes}
  </dashboards>
  <windows source-height='30'>{windows}</windows>
</workbook>
"""


def main() -> None:
    build_extract()
    TWB.parent.mkdir(parents=True, exist_ok=True)
    TWB.write_text(workbook(str(CSV.parent).replace("\\", "/"),
                            str(HYPER).replace("\\", "/")), encoding="utf-8")
    # Packaged workbook: data travels inside the file, so it opens on any machine.
    with zipfile.ZipFile(TWBX, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(TWB.name, workbook("Data/clean", "Data/Extracts/uber_rides.hyper"))
        z.write(CSV, "Data/clean/uber_rides_clean.csv")
        z.write(HYPER, "Data/Extracts/uber_rides.hyper")
    print(f"Wrote {TWB}\nWrote {TWBX}")


if __name__ == "__main__":
    main()
