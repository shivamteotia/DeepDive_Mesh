# The hackathon's `workflow.yaml` goes in this directory

Download it from the Agent Arena 2026 portal → **Problem Statements** tab → scroll to the
submission steps → it offers `workflow.yaml`. Save it here as `.github/workflows/workflow.yaml`,
commit, and push.

Without it the GitHub Action never runs and **the submission is never collected**, however
good the code is.

Then add two repository secrets (Settings → Secrets and variables → Actions):

| secret | value |
|---|---|
| `MESH_API_KEY` | the `rsk_...` Router Service Key (same one as `Mesh_AI_API_Key` in `.env`) |
| `SUBMISSION_TOKEN` | the token the portal gave on registration (it is in `.env` as `Submission_Token`) |

The action checks the submission token to form the unique submission id, checks the Mesh API
key is present, and checks the project builds. Delete this file once the real yaml is in place.
