//! Criterion benchmarks for row hydration at 1 / 10 / 100 / 1000 rows.
//!
//! Budget (ARCHITECTURE.md §14 / benches/README.md): `hydrate_rows` p99 < 5 ms @ 100 rows.

use criterion::{black_box, criterion_group, criterion_main, BatchSize, BenchmarkId, Criterion};
use ferrum_core::hydrate::hydrate_rows;
use ferrum_core::hydrate::RowPayload;
use ferrum_core::ir::metadata::{FieldMeta, FieldType, ModelMetadata};
use serde_json::json;

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
            },
            FieldMeta {
                name: "title".into(),
                column_name: "title".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into(), "icontains".into()],
                nullable: false,
                vector_dimensions: None,
            },
            FieldMeta {
                name: "body".into(),
                column_name: "body".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into()],
                nullable: true,
                vector_dimensions: None,
            },
            FieldMeta {
                name: "published".into(),
                column_name: "published".into(),
                field_type: FieldType::Bool,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
            },
        ],
        pk_index: 0,
    }
}

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

criterion_group!(benches, hydrate_rows_bench);
criterion_main!(benches);
