"""Disk clean-up tools (beyond what Windows offers natively).

Every tool scans first and returns ``Candidate`` items; nothing changes until
``actions.apply`` runs on the ones the user selected. Every change is written to
the history journal so it can be undone.
"""
