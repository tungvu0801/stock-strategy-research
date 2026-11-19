"""research_gate/ - the research admission gate (I-1B-1): md_eval evaluates only a dataset this package admitted.

  admit(provider)                    an MD-A SyntheticProvider -> an AdmittedDataset (canonical snapshot, recomputed acceptance record and
                                     decision, the pinned synthetic allow-list; every HISTORICAL dataset refuses)
  admit_from_source(adapter_id, root) an MD-A adapter built by the gate itself from a source root (read-only, under that root only)
  admit_double(double)               an MD-B contract double (SYNTHETIC and pinned; a deep-copied snapshot)
  admit_for_engine(obj, admission=)  md_eval.engine.run's dispatcher (re-verifies an issued AdmittedDataset field by field)
  verify(ds), require_admitted(x)    the snapshot unchanged (and, source-built, the source files unchanged)
  require_admitted_view(v)           a decision view this package issued (AdmittedView), for md_eval's signal sources
  manifest_view, admitted_manifest, identity_fields, read_log, read_guard; the refusal exceptions and their reason codes

An admission is a gate for COMMITTED evaluation code on SYNTHETIC data. It is not a security sandbox: in-process code can still reach the
gate's private state (residual risk RR-1), and the verdict's admission check is a consistency check only (RR-2)."""
from research_gate.snapshot import (ADMISSION_MISMATCH, ALLOWLIST_GUARD_FAILED, BRIDGE_UNMAPPABLE, CALENDAR_WARMUP_FAILED, CAP_EXCEEDED,
                                    CONTENT_HASH_STALE, CONTRACT_MISMATCH, COUNTS_MISMATCH, DATASET_BLOCKED, DOUBLE_HISTORICAL_NOT_ADMITTED,
                                    FILE_READ_NOT_ALLOWED, GUARD_NESTED, GUARD_ROOT_NOT_ALLOWED, INTEGRITY_ONLY_BAR, LICENCE_EVIDENCE_UNDEFINED,
                                    NOT_A_REGISTERED_PROVIDER, NOT_ADMITTED, NOT_ADMITTED_VIEW, NOT_EVALUATION, OFFLINE_VIOLATION,
                                    PREFIX_NOT_SYNTHETIC, REASON_CODES, RECORD_INVALID, ROOT_INSIDE_REPOSITORY, ROOT_MISSING, ROOT_NOT_EMPTY,
                                    SIDE_CHANNEL_MISMATCH, SNAPSHOT_CHANGED, SNAPSHOT_ROUNDTRIP_FAILED, SOURCE_CHANGED_DURING_RUN, SOURCE_UNREADABLE,
                                    SYMBOL_NOT_INVENTED, SYNTHETIC_NOT_PINNED, AdmissionRefused, FingerprintError, NotAdmitted)
from research_gate.bridge import AdmittedView, require_admitted_view
from research_gate.admit import (ADMITTED_AS_REACHABLE, Admission, AdmittedDataset, admit, admit_double, admit_for_engine, admit_from_source,
                                 admitted_manifest, identity_fields, manifest_view, read_guard, read_log, require_admitted, verify)
