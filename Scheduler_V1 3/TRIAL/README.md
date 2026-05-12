# Trial App

This folder is a separate launcher for the trial scheduler.

Run it from this folder so it uses its own database:

```powershell
python .\run.py
```

It will create and use `trial.db` in this folder, separate from the main scheduler database.
