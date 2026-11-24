"""Local exploration API. No authentication service. CORS is limited to localhost."""

from __future__ import annotations

from pathlib import Path

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from shared.contracts import (
    BaselineReport,
    BaselineRequest,
    BranchRecord,
    Checkpoint,
    ConvergenceDecision,
    ConvergenceEvaluateRequest,
    CreateBranchRequest,
    CreateInvestigationRequest,
    ExplorationExport,
    Investigation,
    InvestigationList,
    InvestigationTrace,
    KnowledgeAssertionIn,
    KnowledgeGraph,
)

from exploration.backend.trace import add_assertion, build_trace, create_branch, knowledge_graph, resume_branch

from exploration.backend.baseline import run_baseline
from exploration.backend.config import default_db_path
from exploration.backend.focus_bridge import focus_http_response, focus_post
from exploration.backend.orchestrator import Orchestrator

LOCAL_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]


def allowed_origins() -> list[str]:
    """Local origins plus any listed in SEARCHHAX_ALLOWED_ORIGINS (comma-separated). Never "*"."""
    extra = [item.strip() for item in os.environ.get("SEARCHHAX_ALLOWED_ORIGINS", "").split(",")]
    return LOCAL_ORIGINS + [item for item in extra if item and item != "*"]


def create_app(db_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="FrontierGraph Exploration", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    # Resolve the convergence decision through the focus ranker in this same
    # process. The /api/convergence/evaluate HTTP route still exists for
    # external callers, but stepping an investigation on this server must not
    # make a blocking HTTP call back into the same worker.
    app.state.orchestrator = Orchestrator(db_path or default_db_path(), post=focus_post)

    app.state.key_locks = {}
    app.state.key_locks_guard = threading.Lock()
    app.state.running = set()
    app.state.running_guard = threading.Lock()

    def idempotent(key: str | None, route: str, body: object, model, fn):
        """Replay the stored response for a repeated Idempotency-Key instead of acting twice.

        Failed requests are not stored, so a retry after an error runs again.
        """
        if not key:
            return fn()
        if len(key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key is longer than 200 characters")
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        with app.state.key_locks_guard:
            lock = app.state.key_locks.setdefault((key, route), threading.Lock())
        with lock:
            database = app.state.orchestrator.database
            prior = database.get_idempotent(key, route)
            if prior is not None:
                if prior[0] != request_hash:
                    raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different request")
                return model.model_validate_json(prior[1])
            result = fn()
            database.put_idempotent(key, route, request_hash, result.model_dump_json(), datetime.now(timezone.utc).isoformat())
            return result

    @contextmanager
    def single_flight(investigation_id: str):
        """At most one research step per investigation at a time; a second one is refused (409)."""
        with app.state.running_guard:
            if investigation_id in app.state.running:
                raise HTTPException(status_code=409, detail="A research step is already running for this investigation")
            app.state.running.add(investigation_id)
        try:
            yield
        finally:
            with app.state.running_guard:
                app.state.running.discard(investigation_id)

    @app.post("/api/exploration/investigations", response_model=Investigation)
    def create_investigation(
        body: CreateInvestigationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Investigation:
        """Store a new question. Local only: no search, fetch or LLM call until /step."""
        if not body.question.strip():
            # min_length=1 accepts whitespace; a blank question is not stored.
            raise HTTPException(status_code=422, detail="question must not be blank")
        profile = body.profile
        source_id = body.profile_from_investigation_id
        if source_id is not None:
            if body.profile is not None:
                raise HTTPException(status_code=422, detail="give either profile or profile_from_investigation_id, not both")
            try:
                profile = app.state.orchestrator.stored_profile(source_id)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"investigation {source_id} not found") from None
            if profile is None:
                # Nothing is invented: without a stored profile the request is refused.
                raise HTTPException(status_code=422, detail=f"investigation {source_id} has no stored research profile")
        return idempotent(
            idempotency_key,
            "create_investigation",
            body.model_dump(mode="json"),
            Investigation,
            lambda: app.state.orchestrator.create(
                body.question, body.budget, profile=profile, profile_source_investigation_id=source_id
            ),
        )

    @app.get("/api/health")
    def health() -> dict:
        """Liveness plus a read of the database. Reports configuration presence, never values."""
        from exploration.backend.config import billable_allowed, llm_api_key, telem_configured
        from shared.contracts import CONTRACT_VERSION

        try:
            app.state.orchestrator.database.list_investigations(limit=1, offset=0)
            database = "ok"
        except Exception as exc:  # reported, not raised: the probe must answer
            database = f"error: {type(exc).__name__}"
        return {
            "status": "ok" if database == "ok" else "degraded",
            "database": database,
            "contract_version": CONTRACT_VERSION,
            "search_configured": telem_configured(),
            "llm_configured": llm_api_key() is not None,
            "billable_allowed": billable_allowed(),
        }

    @app.get("/api/exploration/investigations", response_model=InvestigationList)
    def list_investigations(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> InvestigationList:
        """Read-only listing of stored investigations, newest first."""
        items, total = app.state.orchestrator.database.list_investigations(limit=limit, offset=offset)
        return InvestigationList.model_validate({"items": items, "total": total, "limit": limit, "offset": offset})

    @app.get("/api/exploration/investigations/{investigation_id}", response_model=ExplorationExport)
    def get_investigation(investigation_id: str) -> ExplorationExport:
        return _load(app, investigation_id)

    def require_billable() -> None:
        from exploration.backend.config import billable_allowed

        if not billable_allowed():
            raise HTTPException(
                status_code=403,
                detail="Billable research is disabled on this server (SEARCHHAX_ALLOW_BILLABLE is false). Nothing was run.",
            )

    @app.post("/api/exploration/investigations/{investigation_id}/step", response_model=ExplorationExport)
    def step_investigation(
        investigation_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ExplorationExport:
        require_billable()
        _load(app, investigation_id)

        def run():
            with single_flight(investigation_id):
                return app.state.orchestrator.step(investigation_id)

        return idempotent(idempotency_key, f"step:{investigation_id}", {}, ExplorationExport, run)

    @app.get("/api/exploration/investigations/{investigation_id}/graph", response_model=ExplorationExport)
    def get_graph(investigation_id: str) -> ExplorationExport:
        return _load(app, investigation_id)

    @app.get("/api/exploration/investigations/{investigation_id}/checkpoints", response_model=list[Checkpoint])
    def get_checkpoints(investigation_id: str) -> list[Checkpoint]:
        export = _load(app, investigation_id)
        return export.checkpoints

    @app.get("/api/exploration/investigations/{investigation_id}/export", response_model=ExplorationExport)
    def export_investigation(investigation_id: str) -> ExplorationExport:
        return _load(app, investigation_id)

    @app.post("/api/exploration/baseline", response_model=BaselineReport)
    def baseline(body: BaselineRequest) -> BaselineReport:
        if body.story == "A":  # Story A runs live retrieval; Story C is the offline fixture.
            require_billable()
        _export, report = run_baseline(
            story=body.story,
            configuration=body.configuration,
            db_path=app.state.orchestrator.database.path.parent / f"baseline-{body.story}-{body.configuration}.sqlite",
            post=app.state.orchestrator.post,
        )
        return report

    @app.post("/api/exploration/fixtures/story-c", response_model=ExplorationExport)
    def story_c() -> ExplorationExport:
        return app.state.orchestrator.run_story_c()

    @app.post("/api/exploration/investigations/{investigation_id}/answer", response_model=ExplorationExport)
    def answer_investigation(investigation_id: str) -> ExplorationExport:
        """Synthesize a cited answer from the investigation's grounded evidence.

        This explains evidence already verified. It adds no facts and does not
        re-verify. Makes one answer-model call.
        """
        require_billable()
        from exploration.backend.answer import compose_answer

        from exploration.backend.budget import BudgetGuard

        export = _load(app, investigation_id)
        guard = BudgetGuard(export.investigation.budget)
        calls_before = guard.budget.llm_calls_used
        answer = compose_answer(export, guard=guard)
        if guard.budget.llm_calls_used > calls_before:
            # The answer-model call counts against the investigation's LLM budget.
            export.investigation.budget = guard.budget
            export.investigation.usage.llm_calls += guard.budget.llm_calls_used - calls_before
        stored = export.investigation.metadata.get("answer")
        if answer.blocked and isinstance(stored, dict) and not stored.get("blocked") and stored.get("markdown"):
            # A failed regeneration does not erase the answer already stored;
            # the failure is recorded separately.
            export.investigation.metadata["answer_regeneration_error"] = answer.blocker
        else:
            export.answer = answer
            export.investigation.metadata["answer"] = answer.model_dump(mode="json")
            export.investigation.metadata.pop("answer_regeneration_error", None)
        app.state.orchestrator.database.update_investigation(export.investigation, export.decision)
        return _load(app, investigation_id)

    @app.get("/api/exploration/investigations/{investigation_id}/trace", response_model=InvestigationTrace)
    def get_trace(investigation_id: str) -> InvestigationTrace:
        _load(app, investigation_id)
        return build_trace(app.state.orchestrator.database, investigation_id)

    @app.get("/api/exploration/investigations/{investigation_id}/branches", response_model=list[BranchRecord])
    def get_branches(investigation_id: str) -> list[BranchRecord]:
        _load(app, investigation_id)
        return app.state.orchestrator.database.list_branches(investigation_id)

    @app.post("/api/exploration/investigations/{investigation_id}/branches", response_model=BranchRecord)
    def post_branch(
        investigation_id: str,
        body: CreateBranchRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> BranchRecord:
        _load(app, investigation_id)

        def run():
            try:
                return create_branch(app.state.orchestrator, investigation_id, body)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="checkpoint not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return idempotent(idempotency_key, f"branch:{investigation_id}", body.model_dump(mode="json"), BranchRecord, run)

    @app.post("/api/exploration/branches/{branch_id}/resume", response_model=ExplorationExport)
    def post_resume(
        branch_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ExplorationExport:
        require_billable()
        branch = app.state.orchestrator.database.get_branch(branch_id)
        if branch is None:
            raise HTTPException(status_code=404, detail="branch not found")

        def run():
            with single_flight(branch.child_investigation_id):
                return resume_branch(app.state.orchestrator, branch_id)

        return idempotent(idempotency_key, f"resume:{branch_id}", {}, ExplorationExport, run)

    @app.post("/api/exploration/fixtures/profile", response_model=ExplorationExport)
    def profile_fixture() -> ExplorationExport:
        return app.state.orchestrator.run_profile()

    @app.get("/api/exploration/profile/knowledge-graph", response_model=KnowledgeGraph)
    def get_knowledge_graph(
        investigation_id: str | None = Query(default=None),
        profile_id: str | None = Query(default=None),
    ) -> KnowledgeGraph:
        if not investigation_id and not profile_id:
            raise HTTPException(status_code=400, detail="investigation_id or profile_id is required")
        try:
            return knowledge_graph(
                app.state.orchestrator.database,
                investigation_id=investigation_id,
                profile_id=profile_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="investigation not found") from exc

    @app.post("/api/exploration/profile/knowledge-assertions", response_model=KnowledgeGraph)
    def post_knowledge_assertion(
        body: KnowledgeAssertionIn,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> KnowledgeGraph:
        if body.investigation_id:
            _load(app, body.investigation_id)
        return idempotent(
            idempotency_key,
            "knowledge_assertion",
            body.model_dump(mode="json"),
            KnowledgeGraph,
            lambda: add_assertion(app.state.orchestrator.database, body),
        )

    @app.post("/api/convergence/evaluate", response_model=ConvergenceDecision)
    def convergence_evaluate(body: ConvergenceEvaluateRequest) -> ConvergenceDecision:
        """Serve the convergence decision from the focus ranker.

        ``decision_source`` is set to ``convergence`` because this body came
        from the controller endpoint. Exploration records it as such only when
        it actually consumes this response.
        """
        decision = focus_http_response(body.model_dump(mode="json"))
        decision.setdefault("decision_source", "convergence")
        return ConvergenceDecision.model_validate(decision)

    return app


def _load(app: FastAPI, investigation_id: str) -> ExplorationExport:
    try:
        return app.state.orchestrator.get(investigation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="investigation not found") from exc


app = create_app()
