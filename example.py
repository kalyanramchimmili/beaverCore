"""GitHub REST API client — a worked example built on beavercore.

The whole file is business logic. Retries, exponential backoff, 429 handling,
connection pooling, and error classification are inherited from ``Client``.
"""

import os

from beavercore import Client, RetryPolicy


def github_client(
    token: str,
    *,
    base_url: str = "https://api.github.com",
    api_version: str = "2022-11-28",
) -> Client:
    if not token:
        raise ValueError("token is required")

    def apply_auth(request_kwargs: dict) -> None:
        headers = request_kwargs.setdefault("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Accept", "application/vnd.github+json")
        headers.setdefault("X-GitHub-Api-Version", api_version)

    return Client(
        base_url=base_url,
        auth=apply_auth,
        retry=RetryPolicy(max_attempts=4),
    )


def get_authenticated_user(client: Client) -> dict:
    return client.get("/user").json()


def get_user(client: Client, username: str) -> dict:
    return client.get(f"/users/{username}").json()


def list_repos(
    client: Client,
    username: str,
    *,
    per_page: int = 30,
    page: int = 1,
) -> list[dict]:
    return client.get(
        f"/users/{username}/repos",
        params={"per_page": per_page, "page": page},
    ).json()


def get_repo(client: Client, owner: str, repo: str) -> dict:
    return client.get(f"/repos/{owner}/{repo}").json()


def rate_limit(client: Client) -> dict:
    return client.get("/rate_limit").json()


if __name__ == "__main__":
    try:
        token = os.environ["GITHUB_TOKEN"]
    except KeyError:
        raise SystemExit("GITHUB_TOKEN environment variable is required") from None

    with github_client(token) as gh:
        me = get_authenticated_user(gh)
        print(f"logged in as {me['login']}")
        for repo in list_repos(gh, me["login"], per_page=10):
            print(f"  - {repo['full_name']}")
