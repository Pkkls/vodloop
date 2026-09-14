# Operator's notes

The README says how the system is built. These pages say how it breaks, how to
tell one break from another that looks identical, and which of your own
measurements are lying to you while you do it.

Everything here was paid for once. The point of writing it down is that it is
not paid for twice.

## Start from the symptom

| What you see | Go to |
|---|---|
| Grey or black picture, channel still "live" | [diagnosis.md](diagnosis.md#the-picture-is-grey) |
| Pusher restarting in a loop | [diagnosis.md](diagnosis.md#the-pusher-restarts-forever) |
| Buffer empty, prep looks stuck | [diagnosis.md](diagnosis.md#the-buffer-is-empty) |
| Nothing new arrives in the library | [diagnosis.md](diagnosis.md#nothing-new-arrives) |
| A file plays nowhere but probes clean | [failures.md](failures.md#2026-09-05-and-2026-09-14-packets-with-no-timestamp) |
| Disk full, everything wedged | [capacity.md](capacity.md) |
| You are about to restart something | [operating.md](operating.md#what-a-restart-costs) |

## The pages

- **[diagnosis.md](diagnosis.md)** The symptom index. For each one: the question
  that settles it, the exact command, and how to read the answer. Written to be
  followed top to bottom without thinking, because it is used at four in the
  morning.
- **[failures.md](failures.md)** Every fault this system has actually had, dated,
  with the cause, the measurement that proved it, the fix, and the control that
  proves the fix still holds. Read the headings once so you recognise a repeat.
- **[probes-that-lie.md](probes-that-lie.md)** The measurements that gave a
  confident wrong answer. This is the page that saves the most time, because a
  bad probe does not fail, it sends you somewhere else for an hour.
- **[operating.md](operating.md)** What you may restart and what each restart
  costs, how to deploy, and the escaping rules in cron and systemd that have
  silently broken things twice.
- **[capacity.md](capacity.md)** Disk arithmetic. How to size a share, why the
  floor exists, and the deadlock that appears when the janitor has nothing it is
  allowed to delete.

## Three rules that generate most of the others

**Measure before you explain.** A cause you reasoned your way to is a guess with
good grammar. Every entry in these pages has a command next to it because the
command is the part that was actually worth keeping.

**A check needs a control.** A test that only ever sees the healthy case passes
on a function that has stopped answering. Every check described here comes with
the negative case that must go red, and where one is missing that is called out
as a gap rather than left implied.

**Nothing is fixed until it has been seen working in the running system.** A
deployed patch does not fix a process that is already running with the old code
in memory, and a green test proves the code, not the chain.
