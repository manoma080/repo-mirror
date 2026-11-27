"""Bounded GitHub GraphQL client with shared cooldowns and cost accounting."""

import json
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import (
    _MAX_RATE_WAIT,
    SourceAPI,
    _is_rate_limited,
    _publish_cooldown_delay,
    _publish_exhausted_response,
    _publish_limit,
    _wait_for_cooldown,
    https_origin,
    record_api_call,
    record_graphql_points,
)
from repo_mirror.logging_utils import timed

GRAPHQL_URL = GITHUB + "/graphql"
GRAPHQL_HOST = "api.github.com"

MAX_REPOS_PER_QUERY = 100
MAX_RELEASES_PER_REPO = 100
MAX_ASSETS_PER_RELEASE = 100
DEFAULT_ASSETS_PER_RELEASE = 0
NODES_PER_POINT = 100
DEFAULT_MAX_POINTS = 20
HARD_MAX_POINTS = 20
MAX_PAGINATION_PAGES = 10_000

_RATE_LIMIT_SELECTION = "rateLimit { cost remaining resetAt }"

_REPOSITORY_FIELDS = (
    "databaseId name nameWithOwner description owner { login } url "
    "isPrivate isArchived isEmpty isDisabled "
    "defaultBranchRef { name target { oid } } "
    "pushedAt updatedAt"
)

_ASSET_FIELDS = "databaseId name size contentType downloadUrl createdAt updatedAt"

_RELEASE_FIELDS_BASE = (
    "databaseId tagName name description isDraft isPrerelease createdAt publishedAt updatedAt"
)


class GraphQLError(RuntimeError):
    """GraphQL failure carrying a fixed log-safe message and unlogged provider details."""

    def __init__(self, safe_message, *, error_types=(), provider_errors=()):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.error_types = tuple(error_types)
        self.provider_errors = tuple(provider_errors)
        self.provider_messages = tuple(
            err.get("message")
            for err in self.provider_errors
            if isinstance(err, dict) and isinstance(err.get("message"), str)
        )

    def __str__(self):
        return self.safe_message

    def __repr__(self):
        return f"GraphQLError({self.safe_message!r})"


def _map_graphql_errors(errors):
    error_types = []
    for err in errors:
        if isinstance(err, dict):
            err_type = err.get("type")
            if isinstance(err_type, str) and err_type:
                error_types.append(err_type.upper())

    if "RATE_LIMITED" in error_types:
        safe_message = "GraphQL request rate limited"
    elif any(t in ("FORBIDDEN", "INSUFFICIENT_SCOPES") for t in error_types):
        safe_message = "GraphQL request forbidden"
    elif "NOT_FOUND" in error_types:
        safe_message = "GraphQL resource not found"
    elif "MAX_NODE_LIMIT_EXCEEDED" in error_types:
        safe_message = "GraphQL query node limit exceeded"
    else:
        safe_message = "GraphQL request returned errors"

    return GraphQLError(safe_message, error_types=error_types, provider_errors=errors)


def _validate_positive_int(value, minimum=1, maximum=None):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid GraphQL query parameters")
    if maximum is not None and value > maximum:
        raise ValueError("invalid GraphQL query parameters")
    return value


def _validate_repo_ref(owner, name):
    if (
        not isinstance(owner, str)
        or not isinstance(name, str)
        or not NAME.fullmatch(owner)
        or not NAME.fullmatch(name)
        or owner in (".", "..")
        or name in (".", "..")
    ):
        raise ValueError("invalid GraphQL query parameters")
    return owner, name


def _validate_repos_list(repos):
    if not isinstance(repos, (list, tuple)) or not repos or len(repos) > MAX_REPOS_PER_QUERY:
        raise ValueError("invalid GraphQL query parameters")
    normalized = []
    for item in repos:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("invalid GraphQL query parameters")
        normalized.append(_validate_repo_ref(item[0], item[1]))
    return normalized


def estimate_query_points(total_nodes):
    """Estimate GraphQL point cost using GitHub's documented scoring formula.

    We budget against GitHub's documented rule (total requested nodes / 100,
    minimum 1, rounded up so boundary queries are never underestimated).
    GitHub's measured connection-call cost is currently cheaper, but the client
    must survive the documented model rather than bet on the measured one.
    """
    nodes = _validate_positive_int(total_nodes, minimum=1)
    return max(1, (nodes + NODES_PER_POINT - 1) // NODES_PER_POINT)


def _clamp_max_points(max_points):
    cap = _validate_positive_int(max_points, minimum=1)
    return min(cap, HARD_MAX_POINTS)


def _validate_point_budget(total_nodes, max_points):
    cap = _clamp_max_points(max_points)
    estimated_points = estimate_query_points(total_nodes)
    if estimated_points > cap:
        raise ValueError("GraphQL query estimated point cost exceeds budget")
    return estimated_points, total_nodes


def _is_mutation_query(query):
    for line in query.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.startswith("mutation")
    return False


def _parse_reset_delay(reset_at):
    if not isinstance(reset_at, str) or not reset_at:
        return None
    try:
        dt = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp() - time.time()


def build_repositories_query(repos, *, max_points=DEFAULT_MAX_POINTS):
    """Build a bounded GraphQL query for up to 100 repositories' metadata."""
    normalized = _validate_repos_list(repos)
    _validate_point_budget(len(normalized), max_points)

    var_defs = []
    fields = []
    variables = {}
    aliases = []
    for index, (owner, name) in enumerate(normalized):
        alias = f"r{index}"
        owner_var = f"owner{index}"
        name_var = f"name{index}"
        var_defs.append(f"${owner_var}: String!, ${name_var}: String!")
        fields.append(f"{alias}: repository(owner: ${owner_var}, name: ${name_var}) {{ {_REPOSITORY_FIELDS} }}")
        variables[owner_var] = owner
        variables[name_var] = name
        aliases.append(alias)

    query = f"query({', '.join(var_defs)}) {{ {' '.join(fields)} {_RATE_LIMIT_SELECTION} }}"
    return query, variables, aliases


def build_releases_query(
    repos,
    *,
    releases_first=MAX_RELEASES_PER_REPO,
    assets_first=DEFAULT_ASSETS_PER_RELEASE,
    cursors=None,
    max_points=DEFAULT_MAX_POINTS,
):
    """Build a bounded GraphQL query for repository releases and optional release assets."""
    normalized = _validate_repos_list(repos)
    releases_count = _validate_positive_int(releases_first, minimum=1, maximum=MAX_RELEASES_PER_REPO)
    assets_count = _validate_positive_int(assets_first, minimum=0, maximum=MAX_ASSETS_PER_RELEASE)
    total_nodes = len(normalized) * (1 + releases_count * (1 + assets_count))
    _validate_point_budget(total_nodes, max_points)

    if cursors is not None:
        if not isinstance(cursors, (list, tuple)) or len(cursors) != len(normalized):
            raise ValueError("invalid GraphQL query parameters")
        for cursor in cursors:
            if cursor is not None and (not isinstance(cursor, str) or not cursor):
                raise ValueError("invalid GraphQL pagination cursor")
    else:
        cursors = [None] * len(normalized)

    var_defs = ["$releasesFirst: Int!"]
    variables = {"releasesFirst": releases_count}
    if assets_count > 0:
        var_defs.append("$assetsFirst: Int!")
        variables["assetsFirst"] = assets_count
        asset_block = (
            f" releaseAssets(first: $assetsFirst) {{ "
            f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_ASSET_FIELDS} }} }}"
        )
    else:
        asset_block = ""

    fields = []
    aliases = []
    for index, (owner, name) in enumerate(normalized):
        alias = f"r{index}"
        owner_var = f"owner{index}"
        name_var = f"name{index}"
        after_var = f"after{index}"
        var_defs.append(f"${owner_var}: String!, ${name_var}: String!, ${after_var}: String")
        fields.append(
            f"{alias}: repository(owner: ${owner_var}, name: ${name_var}) {{ "
            f"releases(first: $releasesFirst, after: ${after_var}, orderBy: {{field: CREATED_AT, direction: DESC}}) {{ "
            f"pageInfo {{ hasNextPage endCursor }} "
            f"nodes {{ {_RELEASE_FIELDS_BASE}{asset_block} }} "
            f"}} }}"
        )
        variables[owner_var] = owner
        variables[name_var] = name
        variables[after_var] = cursors[index]
        aliases.append(alias)

    query = f"query({', '.join(var_defs)}) {{ {' '.join(fields)} {_RATE_LIMIT_SELECTION} }}"
    return query, variables, aliases


def build_release_assets_query(
    owner,
    name,
    tag_name,
    *,
    assets_first=MAX_ASSETS_PER_RELEASE,
    after=None,
    max_points=DEFAULT_MAX_POINTS,
):
    """Build a bounded GraphQL query for one release's assets."""
    owner, name = _validate_repo_ref(owner, name)
    if not isinstance(tag_name, str) or not tag_name:
        raise ValueError("invalid GraphQL query parameters")
    if after is not None and (not isinstance(after, str) or not after):
        raise ValueError("invalid GraphQL pagination cursor")
    assets_count = _validate_positive_int(assets_first, minimum=1, maximum=MAX_ASSETS_PER_RELEASE)
    total_nodes = 2 + assets_count
    _validate_point_budget(total_nodes, max_points)

    query = (
        "query($owner: String!, $name: String!, $tagName: String!, $assetsFirst: Int!, $after: String) { "
        "repository(owner: $owner, name: $name) { "
        "release(tagName: $tagName) { "
        "databaseId tagName "
        f"releaseAssets(first: $assetsFirst, after: $after) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {_ASSET_FIELDS} }} }} "
        "} } "
        f"{_RATE_LIMIT_SELECTION} }}"
    )
    variables = {
        "owner": owner,
        "name": name,
        "tagName": tag_name,
        "assetsFirst": assets_count,
        "after": after,
    }
    return query, variables


def normalize_repository_node(node):
    """Convert a GraphQL repository node into a REST-shaped metadata dict."""
    if node is None:
        return None
    if not isinstance(node, dict):
        raise GraphQLError("invalid GraphQL response")

    database_id = node.get("databaseId")
    if type(database_id) is not int or database_id <= 0:
        raise GraphQLError("GraphQL repository response missing integer databaseId")

    description = node.get("description")
    if description is not None and not isinstance(description, str):
        raise GraphQLError("invalid GraphQL response")

    url = node.get("url")
    if url is not None and not isinstance(url, str):
        raise GraphQLError("invalid GraphQL response")

    name = node.get("name")
    if name is not None and not isinstance(name, str):
        raise GraphQLError("invalid GraphQL response")

    full_name = node.get("nameWithOwner")
    if full_name is not None and not isinstance(full_name, str):
        raise GraphQLError("invalid GraphQL response")

    default_branch_ref = node.get("defaultBranchRef")
    if default_branch_ref is None:
        default_branch = None
    elif isinstance(default_branch_ref, dict):
        default_branch = default_branch_ref.get("name")
        if default_branch is not None and not isinstance(default_branch, str):
            raise GraphQLError("invalid GraphQL response")
    else:
        raise GraphQLError("invalid GraphQL response")

    result = {
        "id": database_id,
        "name": name,
        "full_name": full_name,
        "description": description,
        "html_url": url,
        "clone_url": (url.rstrip("/") + ".git") if url else None,
        "default_branch": default_branch,
    }

    owner_node = node.get("owner")
    if isinstance(owner_node, dict) and isinstance(owner_node.get("login"), str):
        result["owner"] = {"login": owner_node["login"]}

    for gql_key, rest_key in (
        ("isPrivate", "private"),
        ("isArchived", "archived"),
        ("isDisabled", "disabled"),
    ):
        val = node.get(gql_key)
        if type(val) is bool:
            result[rest_key] = val

    for gql_key, rest_key in (("pushedAt", "pushed_at"), ("updatedAt", "updated_at")):
        val = node.get(gql_key)
        if isinstance(val, str):
            result[rest_key] = val

    return result


class GraphQLClient:
    """GitHub GraphQL client sharing REST auth, cooldowns, and API call accounting."""

    def __init__(self, api, *, max_points=DEFAULT_MAX_POINTS):
        self._api = api
        self.max_points = _clamp_max_points(max_points)
        self.last_rate_limit = None
        self.last_estimated_points = None
        self.last_estimated_nodes = None

    def _resolve_token(self, parsed):
        origin = getattr(self._api, "trusted_api_origin", None)
        if origin is not None and https_origin(GRAPHQL_URL) != origin:
            raise ValueError("API request outside configured origin")
        if isinstance(self._api, SourceAPI):
            return self._api._request_token("GET", parsed)
        return self._api._request_token("POST", parsed)

    def _record_rate_limit(self, data):
        if not isinstance(data, dict):
            return None
        rate_limit = data.get("rateLimit")
        if not isinstance(rate_limit, dict):
            return None
        self.last_rate_limit = dict(rate_limit)
        cost = rate_limit.get("cost")
        if type(cost) is int and cost >= 0:
            record_graphql_points(cost)
        remaining = rate_limit.get("remaining")
        if type(remaining) is int and remaining == 0:
            delay = _parse_reset_delay(rate_limit.get("resetAt"))
            _publish_cooldown_delay(GRAPHQL_HOST, delay if delay is not None else _MAX_RATE_WAIT)
        return rate_limit

    def _execute(self, query, variables=None, *, estimated_nodes=None, allowed_not_found_aliases=None):
        """Low-level GraphQL POST transport; every call must pass the point-budget gate."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("invalid GraphQL query parameters")
        if variables is not None and not isinstance(variables, dict):
            raise ValueError("invalid GraphQL query parameters")
        if estimated_nodes is None:
            raise ValueError("GraphQL query point estimate is required")
        estimated_points, total_nodes = _validate_point_budget(estimated_nodes, self.max_points)
        self.last_estimated_points = estimated_points
        self.last_estimated_nodes = total_nodes

        parsed = urlparse(GRAPHQL_URL)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("API URL must be credential-free HTTPS")

        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        body = json.dumps(payload).encode("utf-8")

        is_mutation = _is_mutation_query(query)
        role = getattr(self._api, "role", "source")
        budget = [_MAX_RATE_WAIT]
        raw = None

        for attempt in range(4):
            _wait_for_cooldown(GRAPHQL_HOST, budget)
            rate_limited = False
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "repo-mirror",
            }
            token = self._resolve_token(parsed)
            if token:
                headers["Authorization"] = "Bearer " + token

            with timed(f"network target={role} operation=graphql method=POST"):
                try:
                    request = Request(GRAPHQL_URL, data=body, headers=headers, method="POST")
                    _wait_for_cooldown(GRAPHQL_HOST, budget)
                    if attempt > 0:
                        print(f"request retry {attempt}/3", file=sys.stderr, flush=True)
                    request._mirror_metrics = (role, "graphql")
                    request._mirror_metrics_counted = True
                    record_api_call(GRAPHQL_HOST, role, "graphql", "POST")
                    with self._api.opener.open(request, timeout=60) as response:
                        _publish_exhausted_response(GRAPHQL_HOST, response)
                        raw = response.read()
                    break
                except HTTPError as e:
                    e.mirror_operation = (role, "graphql", "POST")
                    limited = _is_rate_limited(e)
                    delay = _publish_limit(GRAPHQL_HOST, e, attempt) if limited else None
                    if (
                        is_mutation
                        or (e.code not in (429, 500, 502, 503, 504) and not limited)
                        or attempt == 3
                        or (limited and delay > _MAX_RATE_WAIT)
                    ):
                        raise
                    if limited:
                        rate_limited = True
                    e.close()
                except URLError as e:
                    e.mirror_operation = (role, "graphql", "POST")
                    if is_mutation or attempt == 3:
                        raise

            if not rate_limited:
                delay = 2**attempt
                if delay > budget[0]:
                    raise RuntimeError("retry wait limit exceeded")
                budget[0] -= delay
                time.sleep(delay)
        else:
            raise RuntimeError("request retries exhausted")

        try:
            decoded = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            raise GraphQLError("invalid GraphQL response")

        if not isinstance(decoded, dict):
            raise GraphQLError("invalid GraphQL response")

        data = decoded.get("data")
        rate_limit = self._record_rate_limit(data)

        errors = decoded.get("errors")
        if errors is not None:
            if not isinstance(errors, list):
                raise GraphQLError("invalid GraphQL response")
            if allowed_not_found_aliases is not None:
                remaining_errors = [
                    err
                    for err in errors
                    if not (
                        isinstance(err, dict)
                        and isinstance(err.get("type"), str)
                        and err["type"].upper() == "NOT_FOUND"
                        and isinstance(err.get("path"), list)
                        and len(err["path"]) == 1
                        and err["path"][0] in allowed_not_found_aliases
                        and isinstance(data, dict)
                        and data.get(err["path"][0]) is None
                    )
                ]
            else:
                remaining_errors = errors
            if remaining_errors:
                mapped = _map_graphql_errors(remaining_errors)
                if "RATE_LIMITED" in mapped.error_types:
                    delay = _parse_reset_delay(rate_limit.get("resetAt")) if rate_limit else None
                    _publish_cooldown_delay(GRAPHQL_HOST, delay if delay is not None else _MAX_RATE_WAIT)
                raise mapped

        if not isinstance(data, dict):
            raise GraphQLError("invalid GraphQL response")
        return data

    def fetch_repositories(self, repos, *, allow_not_found=False):
        """Fetch metadata for up to 100 repositories in a single bounded query."""
        query, variables, aliases = build_repositories_query(repos, max_points=self.max_points)
        allowed = frozenset(aliases) if allow_not_found else None
        data = self._execute(
            query,
            variables,
            estimated_nodes=len(aliases),
            allowed_not_found_aliases=allowed,
        )
        return [data.get(alias) for alias in aliases]

    def fetch_repositories_metadata(self, repos, *, allow_not_found=False):
        """Fetch and normalize repository metadata in batches of up to 100 repositories."""
        deduped = []
        seen = set()
        for owner, name in repos:
            key = (owner.lower(), name.lower())
            if key not in seen:
                seen.add(key)
                deduped.append((owner, name, key))

        if not deduped:
            return {}

        results = {}
        for offset in range(0, len(deduped), MAX_REPOS_PER_QUERY):
            chunk = deduped[offset : offset + MAX_REPOS_PER_QUERY]
            nodes = self.fetch_repositories(
                [(owner, name) for owner, name, _ in chunk],
                allow_not_found=allow_not_found,
            )
            for (_, _, key), node in zip(chunk, nodes):
                results[key] = normalize_repository_node(node)
        return results

    def fetch_repository(self, owner, name):
        """Fetch metadata for a single repository."""
        return self.fetch_repositories([(owner, name)])[0]

    def fetch_releases(
        self,
        owner,
        name,
        *,
        releases_per_page=MAX_RELEASES_PER_REPO,
        assets_per_release=DEFAULT_ASSETS_PER_RELEASE,
        max_pages=MAX_PAGINATION_PAGES,
    ):
        """Fetch releases for one repository using cursor-only pagination."""
        pages_limit = _validate_positive_int(max_pages, minimum=1, maximum=MAX_PAGINATION_PAGES)
        cursor = None
        seen_cursors = set()
        results = []

        for _ in range(pages_limit):
            query, variables, aliases = build_releases_query(
                [(owner, name)],
                releases_first=releases_per_page,
                assets_first=assets_per_release,
                cursors=[cursor],
                max_points=self.max_points,
            )
            estimated_nodes = 1 + releases_per_page * (1 + assets_per_release)
            data = self._execute(query, variables, estimated_nodes=estimated_nodes)
            repo_node = data.get(aliases[0])
            if repo_node is None:
                return results
            if not isinstance(repo_node, dict):
                raise GraphQLError("invalid GraphQL response")
            releases_conn = repo_node.get("releases")
            if not isinstance(releases_conn, dict):
                raise GraphQLError("invalid GraphQL response")
            nodes = releases_conn.get("nodes")
            if not isinstance(nodes, list):
                raise GraphQLError("invalid GraphQL response")
            results.extend(node for node in nodes if isinstance(node, dict))

            page_info = releases_conn.get("pageInfo")
            if not isinstance(page_info, dict):
                raise GraphQLError("invalid GraphQL response")
            has_next = page_info.get("hasNextPage")
            if has_next is False:
                return results
            if has_next is not True:
                raise GraphQLError("invalid GraphQL response")

            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("invalid GraphQL pagination cursor")
            if next_cursor in seen_cursors:
                raise RuntimeError("GraphQL pagination cycle detected")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise RuntimeError("GraphQL pagination exceeded safety limit")

    def fetch_release_assets(
        self,
        owner,
        name,
        tag_name,
        *,
        assets_per_page=MAX_ASSETS_PER_RELEASE,
        max_pages=MAX_PAGINATION_PAGES,
    ):
        """Fetch assets for one release using cursor-only pagination."""
        pages_limit = _validate_positive_int(max_pages, minimum=1, maximum=MAX_PAGINATION_PAGES)
        cursor = None
        seen_cursors = set()
        results = []

        for _ in range(pages_limit):
            query, variables = build_release_assets_query(
                owner,
                name,
                tag_name,
                assets_first=assets_per_page,
                after=cursor,
                max_points=self.max_points,
            )
            data = self._execute(query, variables, estimated_nodes=2 + assets_per_page)
            repo_node = data.get("repository")
            if repo_node is None:
                return results
            if not isinstance(repo_node, dict):
                raise GraphQLError("invalid GraphQL response")
            release_node = repo_node.get("release")
            if release_node is None:
                return results
            if not isinstance(release_node, dict):
                raise GraphQLError("invalid GraphQL response")
            assets_conn = release_node.get("releaseAssets")
            if not isinstance(assets_conn, dict):
                raise GraphQLError("invalid GraphQL response")
            nodes = assets_conn.get("nodes")
            if not isinstance(nodes, list):
                raise GraphQLError("invalid GraphQL response")
            results.extend(node for node in nodes if isinstance(node, dict))

            page_info = assets_conn.get("pageInfo")
            if not isinstance(page_info, dict):
                raise GraphQLError("invalid GraphQL response")
            has_next = page_info.get("hasNextPage")
            if has_next is False:
                return results
            if has_next is not True:
                raise GraphQLError("invalid GraphQL response")

            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ValueError("invalid GraphQL pagination cursor")
            if next_cursor in seen_cursors:
                raise RuntimeError("GraphQL pagination cycle detected")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise RuntimeError("GraphQL pagination exceeded safety limit")
