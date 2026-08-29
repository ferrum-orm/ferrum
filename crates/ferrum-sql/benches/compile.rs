//! Criterion benchmarks for the Rust compile path.
//!
//! Budget (ARCHITECTURE.md §14 / benches/README.md): `compile_query` p99 < 1 ms.
//!
//! Coverage added in W4-B:
//! - `select_filtered` — representative SELECT (original).
//! - `select_join` — `select_related` LEFT JOIN with projected remote fields.
//! - `select_relation_filter` — INNER JOIN filter-only (project_remote=false).
//! - `select_aggregate` — GROUP BY + COUNT/SUM + HAVING.
//! - `select_vector_knn` — pgvector KNN ORDER BY.
//! - `select_text_rank` — full-text `rank_by` ORDER BY.
//! - `select_predicate` — `Q(...)` predicate tree (AND/OR/NOT).
//! - `insert` — single-row INSERT … RETURNING.
//! - `update` — scoped UPDATE … RETURNING.
//! - `delete` — scoped DELETE.
//! - `bulk_insert` — 100-row bulk INSERT … RETURNING.
//! - `bulk_update` — 100-row PK-keyed bulk UPDATE.
//! - `bulk_delete` — 100-row PK-keyed bulk DELETE.

// Bench-harness files are test infrastructure, not shipped code. Keep
// `clippy::all` enforced; suppress the pedantic group (doc backticks,
// cast-precision on bench indices) to keep data construction readable.
#![allow(clippy::pedantic)]

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};
use ferrum_core::ir::{
    metadata::{FieldMeta, FieldType},
    AggregateExpression, AggregateFunction, Aggregation, BindValue, BulkUpdateRow, FieldRef,
    Filter, GroupExpression, Having, HavingOperator, JoinFieldRef, JoinKind, JoinSpec,
    ModelMetadata, Operation, OrderBy, Predicate, QuerySetIR, SortDirection, TextRankBy,
    TextSearchMode, VectorMetric, VectorOrderBy, IR_VERSION,
};
use ferrum_sql::dialect::Dialect;
use ferrum_sql::emit::{
    emit_bulk_delete, emit_bulk_insert, emit_bulk_update, emit_delete, emit_insert, emit_select,
    emit_update,
};

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

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
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "email".into(),
                column_name: "email".into(),
                field_type: FieldType::Text,
                allowed_operators: vec!["eq".into(), "icontains".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "active".into(),
                column_name: "active".into(),
                field_type: FieldType::Bool,
                allowed_operators: vec!["eq".into()],
                nullable: false,
                vector_dimensions: None,
                fts_config: None,
                fts_source_columns: None,
            },
            FieldMeta {
                name: "score".into(),
                column_name: "score".into(),
                field_type: FieldType::Float,
                allowed_operators: vec!["eq".into(), "gt".into()],
                nullable: true,
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

/// Metadata with a vector column for KNN benchmarks.
fn vector_metadata() -> ModelMetadata {
    let mut meta = bench_metadata();
    meta.model_name = "Doc".into();
    meta.table_name = "docs".into();
    meta.fields.push(FieldMeta {
        name: "embedding".into(),
        column_name: "embedding".into(),
        field_type: FieldType::Vector,
        allowed_operators: vec!["eq".into()],
        nullable: true,
        vector_dimensions: Some(8),
        fts_config: None,
        fts_source_columns: None,
    });
    meta
}

/// Metadata with a tsvector column for full-text rank benchmarks.
fn fts_metadata() -> ModelMetadata {
    let mut meta = bench_metadata();
    meta.model_name = "Article".into();
    meta.table_name = "articles".into();
    meta.fields.push(FieldMeta {
        name: "body".into(),
        column_name: "body".into(),
        field_type: FieldType::TsVector,
        allowed_operators: vec!["match".into()],
        nullable: true,
        vector_dimensions: None,
        fts_config: Some("english".into()),
        fts_source_columns: None,
    });
    meta
}

// ---------------------------------------------------------------------------
// IR builders
// ---------------------------------------------------------------------------

fn base_ir(model: &str) -> QuerySetIR {
    QuerySetIR {
        version: IR_VERSION,
        model_name: model.into(),
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
        filters: vec![],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_select_ir() -> QuerySetIR {
    let mut ir = base_ir("User");
    ir.filters = vec![
        Filter {
            field: FieldRef {
                name: "active".into(),
                index: 2,
            },
            operator: "eq".into(),
            value: BindValue::Bool(true),
            join_alias: None,
        },
        Filter {
            field: FieldRef {
                name: "email".into(),
                index: 1,
            },
            operator: "icontains".into(),
            value: BindValue::Text("example.com".into()),
            join_alias: None,
        },
    ];
    ir.order_by = vec![
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
    ];
    ir.limit = Some(50);
    ir.offset = Some(10);
    ir
}

fn bench_select_join_ir() -> QuerySetIR {
    let mut ir = base_ir("User");
    ir.joins = vec![JoinSpec {
        relation: "profile".into(),
        alias: "profile".into(),
        local_field: FieldRef {
            name: "id".into(),
            index: 0,
        },
        remote_table: "profiles".into(),
        remote_pk_column: "user_id".into(),
        remote_fields: vec![
            JoinFieldRef {
                index: 0,
                name: "bio".into(),
                column: "bio".into(),
                allowed_operators: vec!["eq".into()],
                field_type: Some(FieldType::Text),
            },
            JoinFieldRef {
                index: 1,
                name: "avatar".into(),
                column: "avatar".into(),
                allowed_operators: vec!["eq".into()],
                field_type: Some(FieldType::Text),
            },
        ],
        join_kind: JoinKind::Left,
        project_remote: true,
    }];
    ir.filters = vec![Filter {
        field: FieldRef {
            name: "active".into(),
            index: 2,
        },
        operator: "eq".into(),
        value: BindValue::Bool(true),
        join_alias: None,
    }];
    ir
}

fn bench_select_relation_filter_ir() -> QuerySetIR {
    let mut ir = base_ir("User");
    ir.joins = vec![JoinSpec {
        relation: "team".into(),
        alias: "team".into(),
        local_field: FieldRef {
            name: "id".into(),
            index: 0,
        },
        remote_table: "teams".into(),
        remote_pk_column: "id".into(),
        remote_fields: vec![JoinFieldRef {
            index: 0,
            name: "slug".into(),
            column: "slug".into(),
            allowed_operators: vec!["eq".into()],
            field_type: Some(FieldType::Text),
        }],
        join_kind: JoinKind::Inner,
        project_remote: false,
    }];
    ir.filters = vec![Filter {
        field: FieldRef {
            name: "slug".into(),
            index: 0,
        },
        operator: "eq".into(),
        value: BindValue::Text("eng".into()),
        join_alias: Some("team".into()),
    }];
    ir
}

fn bench_select_aggregate_ir() -> QuerySetIR {
    let mut ir = base_ir("User");
    ir.operation = Operation::Select {
        fields: vec![FieldRef {
            name: "active".into(),
            index: 2,
        }],
    };
    ir.aggregation = Some(Aggregation {
        groups: vec![GroupExpression::Field {
            field: FieldRef {
                name: "active".into(),
                index: 2,
            },
        }],
        aggregates: vec![
            AggregateExpression {
                function: AggregateFunction::Count,
                field: None,
                filter: None,
            },
            AggregateExpression {
                function: AggregateFunction::Sum,
                field: Some(FieldRef {
                    name: "score".into(),
                    index: 3,
                }),
                filter: None,
            },
        ],
        having: vec![Having {
            aggregate_index: 1,
            operator: HavingOperator::Gt,
            value: BindValue::Float(100.0),
        }],
    });
    ir
}

fn bench_select_vector_knn_ir() -> QuerySetIR {
    let mut ir = base_ir("Doc");
    ir.operation = Operation::Select {
        fields: vec![
            FieldRef {
                name: "id".into(),
                index: 0,
            },
            FieldRef {
                name: "email".into(),
                index: 1,
            },
        ],
    };
    ir.vector_order_by = Some(VectorOrderBy {
        field: FieldRef {
            name: "embedding".into(),
            index: 4,
        },
        metric: VectorMetric::Cosine,
        value: BindValue::FloatArray(vec![0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]),
    });
    ir.limit = Some(10);
    ir
}

fn bench_select_text_rank_ir() -> QuerySetIR {
    let mut ir = base_ir("Article");
    ir.operation = Operation::Select {
        fields: vec![
            FieldRef {
                name: "id".into(),
                index: 0,
            },
            FieldRef {
                name: "email".into(),
                index: 1,
            },
        ],
    };
    ir.text_rank_by = Some(TextRankBy {
        field: FieldRef {
            name: "body".into(),
            index: 4,
        },
        query: BindValue::Text("postgres performance".into()),
        mode: TextSearchMode::Websearch,
    });
    ir.limit = Some(20);
    ir
}

fn bench_select_predicate_ir() -> QuerySetIR {
    let mut ir = base_ir("User");
    ir.predicate = Some(Predicate::Or {
        children: vec![
            Predicate::And {
                children: vec![
                    Predicate::Filter {
                        filter: Filter {
                            field: FieldRef {
                                name: "active".into(),
                                index: 2,
                            },
                            operator: "eq".into(),
                            value: BindValue::Bool(true),
                            join_alias: None,
                        },
                    },
                    Predicate::Filter {
                        filter: Filter {
                            field: FieldRef {
                                name: "score".into(),
                                index: 3,
                            },
                            operator: "gt".into(),
                            value: BindValue::Float(50.0),
                            join_alias: None,
                        },
                    },
                ],
            },
            Predicate::Not {
                child: Box::new(Predicate::Filter {
                    filter: Filter {
                        field: FieldRef {
                            name: "email".into(),
                            index: 1,
                        },
                        operator: "icontains".into(),
                        value: BindValue::Text("spam".into()),
                        join_alias: None,
                    },
                }),
            },
        ],
    });
    ir
}

fn bench_insert_ir() -> QuerySetIR {
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::Insert {
            values: vec![
                (
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                    BindValue::Text("a@b.com".into()),
                ),
                (
                    FieldRef {
                        name: "active".into(),
                        index: 2,
                    },
                    BindValue::Bool(true),
                ),
                (
                    FieldRef {
                        name: "score".into(),
                        index: 3,
                    },
                    BindValue::Float(1.0),
                ),
            ],
        },
        filters: vec![],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_update_ir() -> QuerySetIR {
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::Update {
            assignments: vec![
                (
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                    BindValue::Text("new@b.com".into()),
                ),
                (
                    FieldRef {
                        name: "score".into(),
                        index: 3,
                    },
                    BindValue::Float(99.0),
                ),
            ],
            danger: false,
        },
        filters: vec![Filter {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            operator: "eq".into(),
            value: BindValue::Int(42),
            join_alias: None,
        }],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_delete_ir() -> QuerySetIR {
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::Delete { danger: false },
        filters: vec![Filter {
            field: FieldRef {
                name: "id".into(),
                index: 0,
            },
            operator: "eq".into(),
            value: BindValue::Int(42),
            join_alias: None,
        }],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_bulk_insert_ir(rows: usize) -> QuerySetIR {
    let row_data: Vec<Vec<(FieldRef, BindValue)>> = (0..rows)
        .map(|i| {
            vec![
                (
                    FieldRef {
                        name: "email".into(),
                        index: 1,
                    },
                    BindValue::Text(format!("u{i}@b.com")),
                ),
                (
                    FieldRef {
                        name: "active".into(),
                        index: 2,
                    },
                    BindValue::Bool(i % 2 == 0),
                ),
                (
                    FieldRef {
                        name: "score".into(),
                        index: 3,
                    },
                    BindValue::Float(f64::from(i as u32)),
                ),
            ]
        })
        .collect();
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::BulkInsert {
            rows: row_data,
            returning: true,
        },
        filters: vec![],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_bulk_update_ir(rows: usize) -> QuerySetIR {
    let pk_fields = vec![FieldRef {
        name: "id".into(),
        index: 0,
    }];
    let fields = vec![
        FieldRef {
            name: "email".into(),
            index: 1,
        },
        FieldRef {
            name: "score".into(),
            index: 3,
        },
    ];
    let rows_data: Vec<BulkUpdateRow> = (0..rows)
        .map(|i| BulkUpdateRow {
            pk_values: vec![BindValue::Int(
                i64::try_from(i).expect("bulk row index") + 1,
            )],
            values: vec![
                BindValue::Text(format!("upd{i}@b.com")),
                BindValue::Float(f64::from(i as u32)),
            ],
        })
        .collect();
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::BulkUpdate {
            pk_fields,
            fields,
            rows: rows_data,
        },
        filters: vec![],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

fn bench_bulk_delete_ir(rows: usize) -> QuerySetIR {
    let pk_fields = vec![FieldRef {
        name: "id".into(),
        index: 0,
    }];
    let ids: Vec<Vec<BindValue>> = (0..rows)
        .map(|i| {
            vec![BindValue::Int(
                i64::try_from(i).expect("bulk row index") + 1,
            )]
        })
        .collect();
    QuerySetIR {
        version: IR_VERSION,
        model_name: "User".into(),
        operation: Operation::BulkDelete { pk_fields, ids },
        filters: vec![],
        order_by: vec![],
        limit: None,
        offset: None,
        vector_order_by: None,
        text_rank_by: None,
        predicate: None,
        distinct: false,
        exists: false,
        joins: vec![],
        aggregation: None,
    }
}

// ---------------------------------------------------------------------------
// Benchmark groups
// ---------------------------------------------------------------------------

fn compile_select_bench(c: &mut Criterion) {
    let metadata = bench_metadata();
    let vector_meta = vector_metadata();
    let fts_meta = fts_metadata();

    let select_ir = bench_select_ir();
    let join_ir = bench_select_join_ir();
    let rel_filter_ir = bench_select_relation_filter_ir();
    let agg_ir = bench_select_aggregate_ir();
    let vector_ir = bench_select_vector_knn_ir();
    let text_ir = bench_select_text_rank_ir();
    let pred_ir = bench_select_predicate_ir();

    let mut group = c.benchmark_group("compile_select");
    group.sample_size(100);
    group.warm_up_time(std::time::Duration::from_millis(500));
    group.measurement_time(std::time::Duration::from_secs(3));

    let cases: [(&str, &ModelMetadata, &QuerySetIR); 7] = [
        ("filtered", &metadata, &select_ir),
        ("join", &metadata, &join_ir),
        ("relation_filter", &metadata, &rel_filter_ir),
        ("aggregate", &metadata, &agg_ir),
        ("vector_knn", &vector_meta, &vector_ir),
        ("text_rank", &fts_meta, &text_ir),
        ("predicate", &metadata, &pred_ir),
    ];

    for (name, meta, ir) in cases {
        let input = (meta, ir);
        group.bench_with_input(BenchmarkId::new("select", name), &input, |b, (meta, ir)| {
            b.iter(|| {
                emit_select(black_box(Dialect::Postgres), black_box(meta), black_box(ir)).unwrap()
            });
        });
    }

    group.finish();
}

fn compile_write_bench(c: &mut Criterion) {
    let metadata = bench_metadata();
    let insert_ir = bench_insert_ir();
    let update_ir = bench_update_ir();
    let delete_ir = bench_delete_ir();

    let mut group = c.benchmark_group("compile_write");
    group.sample_size(100);
    group.warm_up_time(std::time::Duration::from_millis(500));
    group.measurement_time(std::time::Duration::from_secs(3));

    group.bench_with_input(
        BenchmarkId::new("insert", 1),
        &(&metadata, &insert_ir),
        |b, (meta, ir)| {
            b.iter(|| {
                emit_insert(black_box(Dialect::Postgres), black_box(meta), black_box(ir)).unwrap()
            });
        },
    );

    group.bench_with_input(
        BenchmarkId::new("update", 1),
        &(&metadata, &update_ir),
        |b, (meta, ir)| {
            b.iter(|| {
                emit_update(black_box(Dialect::Postgres), black_box(meta), black_box(ir)).unwrap()
            });
        },
    );

    group.bench_with_input(
        BenchmarkId::new("delete", 1),
        &(&metadata, &delete_ir),
        |b, (meta, ir)| {
            b.iter(|| {
                emit_delete(black_box(Dialect::Postgres), black_box(meta), black_box(ir)).unwrap()
            });
        },
    );

    group.finish();
}

fn compile_bulk_bench(c: &mut Criterion) {
    let metadata = bench_metadata();
    let row_counts = [10_usize, 100, 1000];

    let mut group = c.benchmark_group("compile_bulk");
    group.sample_size(50);
    group.warm_up_time(std::time::Duration::from_millis(500));
    group.measurement_time(std::time::Duration::from_secs(3));

    for count in row_counts {
        let bi = bench_bulk_insert_ir(count);
        let bu = bench_bulk_update_ir(count);
        let bd = bench_bulk_delete_ir(count);

        group.bench_with_input(
            BenchmarkId::new("bulk_insert", count),
            &(&metadata, &bi),
            |b, (meta, ir)| {
                b.iter(|| {
                    emit_bulk_insert(black_box(Dialect::Postgres), black_box(meta), black_box(ir))
                        .unwrap()
                });
            },
        );

        group.bench_with_input(
            BenchmarkId::new("bulk_update", count),
            &(&metadata, &bu),
            |b, (meta, ir)| {
                b.iter(|| {
                    emit_bulk_update(black_box(Dialect::Postgres), black_box(meta), black_box(ir))
                        .unwrap()
                });
            },
        );

        group.bench_with_input(
            BenchmarkId::new("bulk_delete", count),
            &(&metadata, &bd),
            |b, (meta, ir)| {
                b.iter(|| {
                    emit_bulk_delete(black_box(Dialect::Postgres), black_box(meta), black_box(ir))
                        .unwrap()
                });
            },
        );
    }

    group.finish();
}

criterion_group!(
    benches,
    compile_select_bench,
    compile_write_bench,
    compile_bulk_bench
);
criterion_main!(benches);
