# Git and Docker


Commit small and often; each commit message should say why, not only what. Use a .gitignore for .env files, virtual environments and caches.

git revert makes a new commit that undoes an old one without rewriting history. A Dockerfile describes how to build an image; docker compose runs several containers together.

Pin dependency versions in requirements so builds are repeatable. Add a health check endpoint such as /healthz so deployments can tell when the app is ready.
