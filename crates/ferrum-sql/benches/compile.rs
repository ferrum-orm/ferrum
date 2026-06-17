//! Criterion benchmarks for the Rust compile path (SELECT emission).
//!
//! Budget (ARCHITECTURE.md §14 / benches/README.md): compile_query p99 < 1 ms.

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};
use ferrum_core::ir::{
    metadata::{FieldMeta, FieldType},
    BindValue, FieldRef, Filter, ModelMetadata, Operation, OrderBy, QuerySetIR, SortDirection,
    IR_VERSION,
};
use ferrum_sql::emit::emit_select;

fn bench_metadata() -> ModelMetadata {
    ModelMetadata {
        model_name: "User".into(),
        table_name: "users".into(),
        fields: vec![
            FieldMeta {
                name: "id".into(),
                column_name: "id".into(),
                field_type: FieldType::Int,
                allowed_operators: vec!["eq".into(), "gt".into(), "lt".into()],
                nullable: false,
                vector_dimensions: None,
            },
            FieldMeta {
                name: "email".into(),
                column_name: "email".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into(), "icontains".into()],
                nullable: false,
                vector_dimensions: None,
            },
            FieldMeta {
                name: "active".into(),
                column_name: "active".into(),
                field_type: FieldType::Bool,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
            },
            FieldMeta {
                name: "score".into(),
                column_name: "score".into(),
                field_type: FieldType::Float,
                allowed_operators: vec!["eq".into(), "gt".into()],
                nullable: true,
                vector_dimensions: None,
            },
        ],
        pk_index: 0,
    }
}

fn bench_select_ir() -> QuerySetIR {
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::Select {
            fields: vec![
                FieldRef {
                    name: "id".into(),
                    index: 0,
                },
                FieldRef {
                    name: "email".into(),
                    index: 1,
                },
                FieldRef {
                    name: "active".into(),
                    index: 2,
                },
            ],
        },
        filters: vec![
            Filter {
                field: FieldRef {
                    name: "active".into(),
                    index: 2,
                },
                operator: "eq".into(),
                value: BindValue::Bool(true),
            },
            Filter {
                field: FieldRef {
                    name: "email".into(),
                    index: 1,
                },
                operator: "icontains".into(),
                value: BindValue::Text("example.com".into()),
            },
        ],
        order_by: vec![
            OrderBy {
                field: FieldRef {
                    name: "score".into(),
                    index: 3,
                },
                direction: SortDirection::Desc,
            },
            OrderBy {
                field: FieldRef {
                    name: "id".into(),
                    index: 0,
                },
                direction: SortDirection::Asc,
            },
        ],
        limit: Some(50),
        offset: Some(10),
        vector_order_by: None,
    }
}

fn compile_query(c: &mut Criterion) {
    let metadata = bench_metadata();
    let ir = bench_select_ir();

    let mut group = c.benchmark_group("compile_query");
    group.sample_size(100);
    group.warm_up_time(std::time::Duration::from_millis(500));
    group.measurement_time(std::time::Duration::from_secs(3));

    group.bench_with_input(
        BenchmarkId::new("select_filtered", "representative"),
        &(&metadata, &ir),
        |b, (meta, query_ir)| {
            b.iter(|| emit_select(black_box(meta), black_box(query_ir)).unwrap());
        },
    );

    group.finish();
}

criterion_group!(benches, compile_query);
criterion_main!(benches);
