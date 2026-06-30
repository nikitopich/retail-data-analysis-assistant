# Retail Data Analysis Assistant

## Run the app

```bash
cp .env.example .env   # fill in GOOGLE_API_KEY and GCP_PROJECT
gcloud auth application-default login

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python main.py
python main.py --debug   # errors / tracebacks
python main.py --trace   # enable Phoenix tracing at http://localhost:6006
```

## Run evals

```bash
# Summary table — easiest way to see results
python -m evals.run                  # all cases (live + faults; live skipped without creds)
python -m evals.run --subset faults  # offline fault cases only (no creds needed)
python -m evals.run --subset live    # live cases only (requires creds + BigQuery)

# pytest
pytest evals/
pytest evals/ -k "D4 or D5 or D6 or C7 or C8 or C9 or C10"  # fault cases only (offline)

# DeepEval
deepeval test run evals/test_assistant.py
```
