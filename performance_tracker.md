# Branching Performance: Proposed Improvements

Two commits on a single branch targeting `netbox-branching` `v0.9.0`.
No changes to NetBox core. All 64 upstream tests pass.

**Fork:** [mrmrcoleman/netbox-branching](https://github.com/mrmrcoleman/netbox-branching)
**Branch:** [`perf/branching-performance`](https://github.com/mrmrcoleman/netbox-branching/compare/main...perf/branching-performance)

**Commits:**
- [`a7dab2b`](https://github.com/mrmrcoleman/netbox-branching/commit/a7dab2b) — Optimize branch provisioning: defer index creation to after data load
- [`cb37178`](https://github.com/mrmrcoleman/netbox-branching/commit/cb37178) — Reduce memory usage and N+1 queries in sync, merge, and revert

**Test dataset:** ~524k objects across ~80 tables (50k devices, 200k interfaces, 200k IPs, 20k VLANs, 2k sites, etc.)
**Benchmark workload:** Mixed CREATE/UPDATE/DELETE — 30% Site creates, 40% Device updates, 30% VLAN deletes

---

## Commit 1: Optimize branch provisioning ([`a7dab2b`](https://github.com/mrmrcoleman/netbox-branching/commit/a7dab2b))

**Files changed:** `netbox_branching/models/branches.py` (1 file, +31 −44)

### Problem

Branch provisioning uses `CREATE TABLE ... INCLUDING INDEXES` for each replicated table, which means PostgreSQL builds every index incrementally as each row is inserted during `INSERT INTO ... SELECT * FROM`. For tables with many indexes and rows, this is significantly slower than loading into an un-indexed heap and building indexes afterward.

Additionally, the provisioning transaction uses `SERIALIZABLE` isolation, which adds overhead (SSI conflict tracking) with no benefit — the new schema is invisible to other sessions until `COMMIT`.

### Changes

1. **Deferred index creation.** Tables are created with `INCLUDING DEFAULTS INCLUDING CONSTRAINTS` (no indexes). After all data is loaded, index definitions are collected from the main schema's `pg_indexes` catalog in a single query and replayed in the branch schema. Each index is built in a single sequential pass over the heap — the optimal path for PostgreSQL.

2. **Removed `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE`.** The default `READ COMMITTED` is sufficient because the branch schema doesn't exist until `COMMIT`.

3. **Eliminated the index rename loop.** The old code created indexes via `INCLUDING INDEXES`, then queried branch indexes, matched them to main by definition, and renamed mismatches. By creating indexes directly from main's catalog, names already match.

### Side effects

**None.** The resulting schema is byte-for-byte identical: same tables, same constraints, same indexes with the same names. Only the creation order changes.

### Benchmark results

| Scale | Upstream `v0.9.0` | With this commit | Speedup |
|-------|-------------------|------------------|---------|
| 524k objects | 7.79s | **2.49s** | **3.1x** |
| 524k objects | 8.95s | **2.76s** | **3.2x** |

Consistent ~3x speedup across runs. The gain scales with number-of-tables × number-of-indexes, so larger schemas (more branchable models) benefit more.

---

## Commit 2: Reduce memory usage in sync/merge/revert ([`cb37178`](https://github.com/mrmrcoleman/netbox-branching/commit/cb37178))

**Files changed:** `netbox_branching/models/branches.py`, `netbox_branching/merge_strategies/iterative.py` (2 files, +36 −20)

### Problem

The sync, merge, and revert code paths evaluate the full changes queryset into a Python list before iterating:

```python
if changes := self.get_unsynced_changes().order_by('time'):
    logger.info(f"Found {len(changes)} changes to sync")
```

This calls `len()` on a queryset, which evaluates and caches every `ObjectChange` row in memory. For branches with thousands of changes, this creates unnecessary memory pressure — particularly relevant for the large-delete merge scenario reported in ENGHLP-1162.

### Changes

1. **`.exists()` + `.count()`** instead of `bool(qs)` / `len(qs)` — checks for changes and logs the count without materializing any rows.

2. **`.iterator()`** on the main processing loop — streams rows from the database cursor instead of caching them in the QuerySet's `_result_cache`. Memory usage drops from O(n) to O(1) for the changes queryset.

3. **`.select_related('changed_object_type')`** — fetches the ContentType in the same query, avoiding a per-row lookup. (ContentType has its own cache so the wall-clock impact is modest, but this avoids redundant cache hits.)

### Side effects

**None.** These are standard Django QuerySet API calls. The same changes are applied, in the same order, to the same objects, through the same `change.apply()` / `change.undo()` code paths. No behavioral changes whatsoever.

### Benchmark results

This commit's primary benefit is **memory**, not wall-clock time. The per-change processing (`change.apply()` → `full_clean()` → `save()` → signal handlers) dominates the loop.

That said, the mixed CREATE/UPDATE/DELETE workload does show a modest improvement at scale — particularly for sync, where avoiding queryset caching and redundant ContentType lookups adds up:

| Operation | Upstream 1k | Optimized 1k | Upstream 5k | Optimized 5k |
|-----------|------------|--------------|------------|--------------|
| Sync | 18.27s | 16.10s (1.1x) | 82.03s | **59.77s (1.4x)** |
| Merge | 20.66s | 17.80s (1.2x) | 80.02s | 68.34s (1.2x) |
| Revert | 32.18s | 23.84s (1.3x) | 125.74s | 107.95s (1.2x) |

The primary value remains **reduced peak memory**: for a 125k-change merge (as in ENGHLP-1162), this avoids loading all 125k `ObjectChange` instances into Python memory at once.

---

## Combined results (both commits, mixed workload)

| Operation | Upstream 1k | Optimized 1k | Speedup | Upstream 5k | Optimized 5k | Speedup |
|-----------|------------|--------------|---------|------------|--------------|---------|
| **Provision** | 7.79s | **2.49s** | **3.1x** | 8.95s | **2.76s** | **3.2x** |
| Sync | 18.27s | 16.10s | 1.1x | 82.03s | **59.77s** | **1.4x** |
| Merge | 20.66s | 17.80s | 1.2x | 80.02s | 68.34s | 1.2x |
| Revert | 32.18s | 23.84s | 1.3x | 125.74s | 107.95s | 1.2x |

Workload: 30% create (Sites) / 40% update (Devices) / 30% delete (VLANs) — per operation.

---

## Test results

All 64 upstream tests pass across all test modules:

| Module | Tests | Result |
|--------|-------|--------|
| `test_branches` | 8 | Pass |
| `test_iterative_merge` | 7 | Pass (1 skipped) |
| `test_sync` | 23 | Pass |
| `test_squash_merge` | 26 | Pass (1 skipped) |
| **Total** | **64** | **All pass** |

---

## What we investigated but chose not to include

We prototyped a `filter(pk).update()` fast path for UPDATE changes that bypasses the full ORM cycle. It achieved dramatic speedups (44x sync, 3-4x merge/revert) but **skips model `save()` overrides and `post_save` signals** that maintain denormalized data on related objects (e.g., cable path recalculation, MPTT tree rebuilds, cached field updates).

Making this safe would require NetBox core to declare which fields trigger cross-object side effects in `save()`. This is not something the branching plugin should maintain as second-hand knowledge — silent drift means silent data corruption.

**This is documented as a future opportunity for NetBox core**, not a plugin concern.

---

## Relevance to ENGHLP-1162 (large-delete merge OOM)

The ENGHLP-1162 OOM (125k DELETE changes causing PostgreSQL to be killed by the OS) is caused by the **single `transaction.atomic()`** that wraps the entire merge. PostgreSQL holds all MVCC dead tuples, WAL entries, and lock state until COMMIT.

Our `.iterator()` change is **strictly positive** for this scenario — it avoids loading all 125k `ObjectChange` instances into Python memory. It does not solve the PostgreSQL-side OOM, which requires transaction splitting (a significant design change to the merge architecture).
