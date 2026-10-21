"""Pydantic request bodies for mutating endpoints."""

from pydantic import BaseModel


class UpdateIn(BaseModel):
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    uri: str | None = None
    tags: list[str] | None = None


class MoveIn(BaseModel):
    new_parent_path: str


class MergeClassesIn(BaseModel):
    target: str                       # category to fold the source class into


class ReparentInstanceIn(BaseModel):
    new_category: str                 # category to move the instance under


class BlockClassIn(BaseModel):
    purge_instances: bool = True      # also delete the class's sub-entities
    requeue_voters: bool = True       # re-enrich the memories that fed the class


class SetClassDescriptionIn(BaseModel):
    description: str


class RetryFailedIn(BaseModel):
    source: str | None = None         # re-queue only this source's failures
    node_ids: list[str] | None = None  # re-queue only these nodes (e.g. the retryable subset)


class DismissStuckIn(BaseModel):
    source: str                        # the stuck record's source
    external_id: str                   # …and its external id (push without one: "inbox#<id>")


class RetireIn(BaseModel):
    reason: str | None = None         # why the memory is being retired (manual override)


class AnalysisExcludeIn(BaseModel):
    retract: bool = False             # also pull this subtree's past entity votes/links


class SignificanceIn(BaseModel):
    value: float                      # additive significance value (cascades when set on a folder)


class CreateIn(BaseModel):
    content: str
    title: str | None = None
    slug: str | None = None
    parent_path: str = ""
    type: str = "note"
    tags: list[str] | None = None
    uri: str | None = None
    summary: str | None = None
    external_id: str | None = None


class ApproveMergeIn(BaseModel):
    target_id: str | None = None      # override the suggested merge target (entity UUID as str)


class RejectMergeIn(BaseModel):
    reason: str | None = None         # why the merge suggestion is being rejected


class SetToolEnabledIn(BaseModel):
    enabled: bool


class SetAgeDecayIn(BaseModel):
    enabled: bool
