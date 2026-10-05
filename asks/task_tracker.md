# Team task tracker API

**Draft ask: edit freely before it is used for a real run.**

Build a REST API in **Python 3.12** for a small team task tracker, using **FastAPI**, **SQLAlchemy 2** with SQLite,
and **pytest** for tests. Use only these libraries: fastapi, pydantic, sqlalchemy, httpx (for the test client),
pyjwt, bcrypt. There is no network access when tests run.

## Features

1. **Accounts and auth.** Register with email and password (passwords stored hashed, never returned). Log in to
   receive a JWT access token. Every other endpoint requires a valid token.
2. **Projects.** An authenticated user can create a project and becomes its owner. Owners can add and remove
   members by email. Users can list only the projects they own or belong to, and cannot see anyone else's.
3. **Tasks.** Within a project, members can create, read, update and delete tasks with a title, description,
   optional assignee (a project member), optional due date, and a status that moves only
   `todo -> in_progress -> done` (and `in_progress -> todo`). Invalid transitions are rejected with a clear error.
   Tasks can be listed with filters for status and assignee.
4. **Comments.** Members can add comments to a task and list them oldest first. Only the author can delete a comment.
5. **Activity log.** Every task create, update, status change and comment is recorded with who did it and when.
   Members can read a project's activity log, newest first.
6. **CSV export.** A project member can download all of a project's tasks as CSV (id, title, status, assignee,
   due date), with values that start with `=`, `+`, `-` or `@` neutralised so spreadsheets cannot run them as formulas.

## Constraints

- Authorisation matters everywhere: a user must never read or change data in a project they are not a member of.
- Every endpoint needs automated tests, including the unhappy paths (unauthenticated, wrong project, invalid input).
- Keep it a single package with clear module boundaries (auth, projects, tasks, comments, activity, export).
