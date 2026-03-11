```
 ██╗     ██╗████████╗      ██████╗ ██╗ █████╗  ██████╗
 ██║     ██║╚══██╔══╝      ██╔══██╗██║██╔══██╗██╔════╝
 ██║     ██║   ██║   █████╗██║  ██║██║███████║██║  ███╗
 ██║     ██║   ██║   ╚════╝██║  ██║██║██╔══██║██║   ██║
 ███████╗██║   ██║         ██████╔╝██║██║  ██║╚██████╔╝
 ╚══════╝╚═╝   ╚═╝         ╚═════╝ ╚═╝╚═╝  ╚═╝ ╚═════╝
```

# Lit-Diag by Lightning AI

**Your GPU cluster health check in one command.**

Tired of running 15 different commands to figure out why your
GPUs are acting up? Yeah, same. So we built this.

---

## What Is This?

Lit-Diag checks your GPU node from top to bottom -- GPUs, network,
storage, thermals, drivers, the whole nine yards -- and tells you
in plain English what's going on. If something's broken, it tells
you what to do. If it's an easy fix, it'll offer to just do it
for you.

We built this for clients first. If you can SSH into a box, you
can run this thing. Zero GPU expertise required. But engineers
love it too because it saves them from asking "hey can you run
nvidia-smi and paste the output" for the hundredth time.

---

## Install

```
pip install .
```

Or install straight from the repo:

```
pip install git+https://github.com/JM-LAI/Lit-diag.git
```

Done. You've got `lit-diag` on your PATH now.

---

## Quick Start

Just type:

```
lit-diag
```

That drops you straight into the interactive menu. Pick option 1
("Run All Checks") and grab a coffee -- takes about 10 seconds.
If some checks need root, it'll ask before running anything.

### Other ways to run it

```
lit-diag run --all              Run all checks (non-interactive)
lit-diag run --all --staff      Full engineer detail
lit-diag run --all --json       Machine-readable output
lit-diag run gpu                Just check GPUs
lit-diag deps                   See which tools are available
lit-diag --version              Version info
lit-diag --help                 Full help
```

---

## What Does It Check?

```
  CHECK             WHAT IT LOOKS AT
  -------------------------------------------------------
  GPU Health        Memory errors, temps, power, clocks
  NVLink            GPU-to-GPU interconnect health
  PCIe Bus          Link speed, width, bus errors
  Kernel Logs       XID errors, crashes, OOM events
  Storage           NVMe health, disk space, unused drives
  Thermal / Power   CPU temps, fans, PSUs, IPMI sensors
  InfiniBand        Network port state, error counters
  CUDA Tests        GPU hardware validation (DCGM)
  NVIDIA Driver     Driver, modules, persistence mode
  System Info       CPU, memory, kernel, NUMA topology
```

---

## How It Works

### First Run

It asks one question: "Are you a client or support staff?"

This controls how much detail you see. Your choice is saved so
you only get asked once. Change it anytime with `--client`,
`--staff`, or `--reset-config`.

### Client View

You get a traffic-light summary:

```
  [OK]  GPU Health
  [OK]  NVLink
  [OK]  PCIe Bus
  [OK]  Storage
  [WARN] NVIDIA Driver

  WARN  NVIDIA kernel module 'nvidia_peermem' not loaded
        The 'nvidia_peermem' kernel module is expected on GPU
        nodes but isn't loaded.

        +-- What to do -----------------------------------+
        |  Quick fix available                            |
        |  Run:  sudo modprobe nvidia_peermem             |
        |  Impact: No downtime, no restart needed.        |
        +-------------------------------------------------+
```

Green = good. Yellow = heads up. Red = needs attention.
Every finding has a "What to do" box so you know exactly
what action to take (or not take).

### Staff View

Same report, same structure -- plus full device inventories,
raw values, NVLink topology, PCIe link details, thermal sensor
readings, and everything an engineer needs to triage without
opening another terminal.

### JSON Output

```
lit-diag run --all --json -o report.json
```

Saves a structured report you can attach to a support ticket.
The engineer on the other end gets all the data regardless of
what you saw on screen.

---

## Auto-Fix

When Lit-Diag finds something it can fix automatically, it
offers to do it:

```
  +-- Quick Fixes Available ----------------------------+
  |                                                     |
  |  1) Load nvidia_peermem module                      |
  |     Run:    sudo modprobe nvidia_peermem            |
  |     Impact: Enables GPU peer memory. No downtime.   |
  |                                                     |
  +-----------------------------------------------------+

  Apply fixes? (enter number, 'all', or 'skip'):
```

It shows you the exact command, what it does, and what the
impact is before it touches anything. Nothing happens without
your say-so.

---

## Root Access

A few checks (kernel logs, thermals, storage health) need root
to get the full picture. Instead of just telling you "run with
sudo" and leaving you to figure it out, it handles the whole
thing:

```
  +-- Root Access -------------------------------------+
  |                                                    |
  |  Some checks need root access for full results:    |
  |                                                    |
  |  Kernel Logs   GPU errors, crashes, system events  |
  |  Thermal       CPU temps, fans, power supply       |
  |  Storage       NVMe health, wear levels, RAID      |
  |                                                    |
  |  Switch to root for the complete picture? (y/N)    |
  +----------------------------------------------------+
```

No fiddling with sudo syntax. Just type y.

---

## GPU Reset

Sometimes you just need to nuke it from orbit. Full GPU reset --
kill processes, unload drivers, PCIe bus reset, reload everything:

```
lit-diag reset-gpu
```

Needs root (obviously). Walks you through every step and
won't do anything destructive without asking first.

---

## Requirements

- Python 3.9+
- Linux (built for GPU cluster nodes)
- NVIDIA drivers installed

### Optional Tools (more checks available if installed)

```
  TOOL          WHAT IT ADDS
  ---------------------------------------------------
  ipmitool      Thermal sensors, fan speeds, PSU health
  nvme-cli      NVMe drive SMART data
  smartmontools Additional disk health data
  lspci         PCIe device enumeration
  ibstat        InfiniBand port details
  dcgmi         NVIDIA GPU hardware validation
  sensors       CPU temperature readings (lm-sensors)
```

Run `lit-diag deps` to see what's available on your system.

---

## Support Workflow

This is the whole reason we built it. One tool, same report,
two audiences. Everybody meets at the same place.

```
  1. Client reports a problem
  2. Engineer says "run lit-diag, option 1, send us the report"
  3. Client runs it, saves the JSON
  4. Engineer opens the JSON -- everything they need is in there
```

No more "can you run nvidia-smi and paste the output?"
No more "now try lspci -vvv and grep for..."
No more "check dmesg for XID errors please."

All of that is already in the report. Every time.

---

## Development

```
git clone https://github.com/JM-LAI/Lit-diag.git
cd Lit-diag
pip install -e ".[dev]"
```

---

*Built by the support team -- because nobody should have to
Google "what is XID 79" at 2am.*
