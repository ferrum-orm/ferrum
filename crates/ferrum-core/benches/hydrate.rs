//! Criterion benchmarks for row hydration at 1 / 10 / 100 / 1000 rows.
//!
//! Budget (ARCHITECTURE.md §14 / benches/README.md): `hydrate_rows` p99 < 5 ms @ 100 rows.
//!
//! Coverage added in W4-B:
//! - `hydrate_rows` across 1 / 10 / 100 / 1000 rows (original).
//! - `hydrate_rows_wide` — 20-column row to measure per-field validation cost
//!   scaling with column count.
//! - `hydrate_rows_jsonb` — JSONB + array + vector field types to cover the
//!   type mix used by ticket-analyzer workloads.
//! - `json_boundary_encode` / `json_boundary_decode` — the throwaway
//!   serde_json copy the PyO3 bridge makes for Rust structural validation
//!   (AGENTS.md: `_RowEncoder` serializes a copy for validation only). This
//!   isolates the JSON wire-format boundary cost from hydration itself.
//! - `validation_copy` — the `validate_row` walk over non-nullable fields,
//!   measured by hydrating the same rows with all-non-nullable vs
//!   all-nullable metadata so the delta is the validation walk.

// Bench-harness files are test infrastructure, not shipped code. Keep
// `clippy::all` enforced; suppress the pedantic group (doc backticks,
// cast-precision on bench indices) to keep data construction readable.
#![allow(clippy::pedantic)]

use criterion::{black_box, criterion_group, criterion_main, BatchSize, BenchmarkId, Criterion};
use ferrum_core::hydrate::hydrate_rows;
use ferrum_core::hydrate::RowPayload;
use ferrum_core::ir::metadata::{FieldMeta, FieldType, ModelMetadata};
use serde_json::json;

// ---------------------------------------------------------------------------
// Metadata builders
// ---------------------------------------------------------------------------

fn bench_metadata() -> ModelMetadata {
    ModelMetadata {
        model_name: "Post".into(),
        table_name: "posts".into(),
        fields: vec![
            FieldMeta {
                name: "id".into(),
                column_name: "id".into(),
                field_type: FieldType::Int,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "title".into(),
                column_name: "title".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into(), "icontains".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "body".into(),
                column_name: "body".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into()],
                nullable: true,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "published".into(),
                column_name: "published".into(),
                field_type: FieldType::Bool,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
        ],
        pk_index: 0,
        pk_fields: vec![0],
        full_text_indexes: vec![],
    }
}

/// 20-column wide model — measures validation cost scaling with column count.
fn wide_metadata(nullable: bool) -> ModelMetadata {
    let fields = (0..20)
        .map(|i| FieldMeta {
            name: format!("col{i}"),
            column_name: format!("col{i}"),
            field_type: if i % 5 == 0 {
                FieldType::Int
            } else if i % 5 == 1 {
                FieldType::Text
            } else if i % 5 == 2 {
                FieldType::Bool
            } else if i % 5 == 3 {
                FieldType::Float
            } else {
                FieldType::Json
            },
            allowed_operators: vec!["eq".into()],
            nullable,
            vector_dimensions: None,
            fts_config: None,
            fts_source_columns: None,
        })
        .collect();
    ModelMetadata {
        model_name: "Wide".into(),
        table_name: "wide".into(),
        fields,
        pk_index: 0,
        pk_fields: vec![0],
        full_text_indexes: vec![],
    }
}

/// JSONB + array + vector field mix — ticket-analyzer workload shape.
fn jsonb_metadata() -> ModelMetadata {
    ModelMetadata {
        model_name: "Doc".into(),
        table_name: "docs".into(),
        fields: vec![
            FieldMeta {
                name: "id".into(),
                column_name: "id".into(),
                field_type: FieldType::Uuid,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "tags".into(),
                column_name: "tags".into(),
                field_type: FieldType::ArrayText,
                allowed_operators: vec!["eq".into(), "contains".into()],
                nullable: true,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "meta".into(),
                column_name: "meta".into(),
                field_type: FieldType::Json,
                allowed_operators: vec!["eq".into(), "contains".into(), "has_key".into()],
                nullable: true,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "embedding".into(),
                column_name: "embedding".into(),
                field_type: FieldType::Vector,
                allowed_operators: vec!["eq".into()],
                nullable: true,
                vector_dimensions: Some(8),
                fts_config: None,
                fts_source_columns: None,
            },
        ],
        pk_index: 0,
        pk_fields: vec![0],
        full_text_indexes: vec![],
    }
}

// ---------------------------------------------------------------------------
// Row builders
// ---------------------------------------------------------------------------

fn make_rows(count: usize) -> Vec<RowPayload> {
    (0..count)
        .map(|i| {
            serde_json::from_value(json!({
                "id": i64::try_from(i).expect("bench row index") + 1,
                "title": format!("Post title {i}"),
                "body": if i % 3 == 0 { serde_json::Value::Null } else { json!(format!("Body {i}")) },
                "published": i % 2 == 0,
            }))
            .expect("valid row payload")
        })
        .collect()
}

fn make_wide_rows(count: usize) -> Vec<RowPayload> {
    (0..count)
        .map(|i| {
            let mut obj = serde_json::Map::new();
            for j in 0..20_u32 {
                let key = format!("col{j}");
                let val = match j % 5 {
                    0 => json!((i as i64) + j as i64),
                    1 => json!(format!("text-{i}-{j}")),
                    2 => json!(j % 2 == 0),
                    3 => json!(i as f64 + 0.5 * j as f64),
                    _ => json!({"k": i, "j": j}),
                };
                obj.insert(key, val);
            }
            obj
        })
        .collect()
}

fn make_jsonb_rows(count: usize) -> Vec<RowPayload> {
    (0..count)
        .map(|i| {
            serde_json::from_value(json!({
                "id": format!("00000000-0000-0000-0000-{:012x}", i),
                "tags": ["alpha", "beta", format!("tag{i}")],
                "meta": {"category": "doc", "weight": i, "nested": {"flag": true}},
                "embedding": [0.1_f64 * i as f64, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            }))
            .expect("valid jsonb row payload")
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Benchmarks
// ---------------------------------------------------------------------------

fn hydrate_rows_bench(c: &mut Criterion) {
    let metadata = bench_metadata();
    let row_counts = [1_usize, 10, 100, 1000];

    let mut group = c.benchmark_group("hydrate_rows");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(300));
    group.measurement_time(std::time::Duration::from_secs(3));

    for count in row_counts {
        let rows = make_rows(count);
        group.bench_with_input(BenchmarkId::new("rows", count), &rows, |b, batch| {
            b.iter_batched(
                || batch.clone(),
                |rows| hydrate_rows(black_box(&metadata), black_box(rows)).unwrap(),
                BatchSize::SmallInput,
            );
        });
    }

    group.finish();
}

fn hydrate_rows_wide_bench(c: &mut Criterion) {
    // All-non-nullable: every column triggers the validation lookup.
    let metadata = wide_metadata(false);
    let rows100 = make_wide_rows(100);

    let mut group = c.benchmark_group("hydrate_rows_wide");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(300));
    group.measurement_time(std::time::Duration::from_secs(3));

    group.bench_with_input(BenchmarkId::new("cols20", 100), &rows100, |b, batch| {
        b.iter_batched(
            || batch.clone(),
            |rows| hydrate_rows(black_box(&metadata), black_box(rows)).unwrap(),
            BatchSize::SmallInput,
        );
    });

    group.finish();
}

fn hydrate_rows_jsonb_bench(c: &mut Criterion) {
    let metadata = jsonb_metadata();
    let row_counts = [1_usize, 100];

    let mut group = c.benchmark_group("hydrate_rows_jsonb");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(300));
    group.measurement_time(std::time::Duration::from_secs(3));

    for count in row_counts {
        let rows = make_jsonb_rows(count);
        group.bench_with_input(BenchmarkId::new("rows", count), &rows, |b, batch| {
            b.iter_batched(
                || batch.clone(),
                |rows| hydrate_rows(black_box(&metadata), black_box(rows)).unwrap(),
                BatchSize::SmallInput,
            );
        });
    }

    group.finish();
}

/// Validation-copy isolation: hydrate the same wide rows under all-nullable
/// metadata (validation walk still iterates but skips the null checks) vs
/// all-non-nullable (full validation). The delta is the validation copy cost.
fn validation_copy_bench(c: &mut Criterion) {
    let meta_nullable = wide_metadata(true);
    let meta_strict = wide_metadata(false);
    let rows = make_wide_rows(100);

    let mut group = c.benchmark_group("validation_copy");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(300));
    group.measurement_time(std::time::Duration::from_secs(3));

    group.bench_with_input(BenchmarkId::new("nullable_meta", 100), &rows, |b, batch| {
        b.iter_batched(
            || batch.clone(),
            |rows| hydrate_rows(black_box(&meta_nullable), black_box(rows)).unwrap(),
            BatchSize::SmallInput,
        );
    });

    group.bench_with_input(BenchmarkId::new("strict_meta", 100), &rows, |b, batch| {
        b.iter_batched(
            || batch.clone(),
            |rows| hydrate_rows(black_box(&meta_strict), black_box(rows)).unwrap(),
            BatchSize::SmallInput,
        );
    });

    group.finish();
}

/// JSON wire-format boundary cost: the throwaway serde_json round-trip the
/// PyO3 bridge performs for Rust structural validation (AGENTS.md: `_RowEncoder`
/// serializes a copy; `model_construct` receives native driver types). This
/// isolates serialization from hydration by encoding rows to JSON bytes and
/// decoding them back, without invoking `hydrate_rows`.
fn json_boundary_bench(c: &mut Criterion) {
    let row_counts = [1_usize, 100, 1000];

    let mut group = c.benchmark_group("json_boundary");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(300));
    group.measurement_time(std::time::Duration::from_secs(3));

    for count in row_counts {
        let rows = make_rows(count);

        group.bench_with_input(BenchmarkId::new("encode", count), &rows, |b, batch| {
            b.iter_batched(
                || serde_json::to_vec(batch).expect("encode"),
                |bytes| {
                    black_box(bytes);
                },
                BatchSize::SmallInput,
            );
        });

        let encoded: Vec<Vec<u8>> = rows
            .iter()
            .map(|r| serde_json::to_vec(r).expect("encode"))
            .collect();
        group.bench_with_input(BenchmarkId::new("decode", count), &encoded, |b, batch| {
            b.iter_batched(
                || batch.clone(),
                |bytes_vec| {
                    let decoded: Vec<RowPayload> = bytes_vec
                        .iter()
                        .map(|b| serde_json::from_slice(b).expect("decode"))
                        .collect();
                    black_box(decoded);
                },
                BatchSize::SmallInput,
            );
        });
    }

    group.finish();
}

criterion_group!(
    benches,
    hydrate_rows_bench,
    hydrate_rows_wide_bench,
    hydrate_rows_jsonb_bench,
    validation_copy_bench,
    json_boundary_bench,
);
criterion_main!(benches);
