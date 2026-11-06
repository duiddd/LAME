//! CSV protocol tests (parser, output format, end-to-end example) through the
//! test-only adapter.

mod common;

use common::{apply_line, format_event, parse_request, Harness, Request};
use match_engine::Side;

fn run(input: &str) -> (String, usize) {
    let mut engine = Harness::new();
    let mut out = String::new();
    let mut invalid = 0;
    for line in input.lines() {
        if !apply_line(&mut engine, line) {
            invalid += 1;
        }
        for e in engine.drain_lines() {
            out.push_str(&format_event(&e));
            out.push('\n');
        }
    }
    (out, invalid)
}

#[test]
fn rejects_malformed_requests() {
    let long_nines = "9".repeat(100);
    let invalid = [
        "",
        " ",
        "BADMESSAGE",
        "2,1",
        "0",
        "1",
        "1,",
        "1,1,",
        "0,1,1,2",
        "0,1,1,2,100,extra",
        "0,1,,2,100",
        "0,1,1,2,100,",
        "0;1,1,2,100",
        "1,1junk",
        "1,1.0",
        "1,0",
        "1,-1",
        "1,+1",
        "1,2147483648",
        "0,1,2,2,100",
        "0,1,-1,2,100",
        "0,1,0.0,2,100",
        "0,1,2147483648,2,100",
        "0,1,1,0,100",
        "0,1,1,-2,100",
        "0,1,1,+2,100",
        "0,1,1,2.0,100",
        "0,1,1,2e1,100",
        "0,1,1,9223372036854775808,100",
        "0,1,1,2,0",
        "0,1,1,2,-1",
        "0,1,1,2,+1",
        "0,1,1,2,nan",
        "0,1,1,2,inf",
        "0,1,1,2,1e2",
        "0,1,1,2,0x10",
        "0,1,1,2,.1",
        "0,1,1,2,1.",
        "0,1,1,2,1.2.3",
        "0,1,1,2,1 00",
        "0,1,1,2,100junk",
        "0,1,1,2,0.00001",
        "0,1,1,2,1.00000",
        "0,1,1,2,922337203685477.5808",
        "0,1,1,2,922337203685477.581",
        "0,1,1,2,922337203685477.6",
        "0,1,1,2,0.0000",
        "0,1,1,2,1..2",
        "0,1,1,2,922337203685478",
        &format!("0,1,1,2,{long_nines}"),
        "0,1,1,2,\x00100",
    ];
    for line in invalid {
        assert_eq!(parse_request(line), None, "line {line:?}");
    }
}

#[test]
fn parses_add_fields_and_whitespace() {
    assert_eq!(
        parse_request(" 0 , 42 , 1 , 123 , 12.3400 \r"),
        Some(Request::Add {
            id: 42,
            side: Side::Sell,
            price: 123400,
            qty: 123
        })
    );
}

#[test]
fn parses_cancel_and_maximum_values() {
    assert_eq!(
        parse_request(" 1 , 2147483647 \r"),
        Some(Request::Cancel {
            id: i32::MAX as u64
        })
    );
    assert_eq!(
        parse_request("0,2147483647,0,9223372036854775807,1"),
        Some(Request::Add {
            id: i32::MAX as u64,
            side: Side::Buy,
            price: 10000,
            qty: i64::MAX
        })
    );
}

#[test]
fn parses_prices_exactly() {
    let cases: [(&str, i64); 12] = [
        ("0.1", 1000),
        ("0.0001", 1),
        ("123.4567", 1234567),
        ("1.2300", 12300),
        ("100.0000", 1000000),
        ("0001.0100", 10100),
        ("1000000.1234", 10000001234),
        ("100000000000.0001", 1000000000000001),
        ("922337203685477", 9223372036854770000),
        ("922337203685477.5807", i64::MAX),
        ("922337203685477.5806", i64::MAX - 1),
        ("922337203685477.580", 9223372036854775800),
    ];
    for (text, scaled) in cases {
        match parse_request(&format!("0,1,0,1,{text}")) {
            Some(Request::Add { price, .. }) => assert_eq!(price, scaled, "price {text}"),
            other => panic!("price {text}: {other:?}"),
        }
    }
    let padded = format!("{}1.0001", "0".repeat(100));
    assert!(matches!(
        parse_request(&format!("0,1,0,1,{padded}")),
        Some(Request::Add { price: 10001, .. })
    ));
}

#[test]
fn keeps_adjacent_prices_distinct() {
    let cases = [
        ("12345.6787", "12345.6788"),
        ("100000000000.0000", "100000000000.0001"),
        ("922337203685477.5806", "922337203685477.5807"),
    ];
    for (lo, hi) in cases {
        let (Some(Request::Add { price: a, .. }), Some(Request::Add { price: b, .. })) = (
            parse_request(&format!("0,1,0,1,{lo}")),
            parse_request(&format!("0,2,1,1,{hi}")),
        ) else {
            panic!("did not parse {lo} / {hi}");
        };
        assert!(a < b, "{lo} < {hi}");
    }
}

#[test]
fn output_format_and_event_order() {
    let (out, invalid) = run("0,1,1,2,0.1\n0,2,0,3,0.2\n");
    assert_eq!((out.as_str(), invalid), ("2,2,0.1\n4,2,1\n3,1\n", 0));
}

#[test]
fn worked_example_with_invalid_line() {
    let input = "0,1000000,1,1,1075\n0,1000001,0,9,1000\n0,1000002,0,30,975\n\
                 0,1000003,1,10,1050\n0,1000004,0,10,950\nBADMESSAGE\n\
                 0,1000005,1,2,1025\n0,1000006,0,1,1000\n1,1000004\n\
                 0,1000007,1,5,1025\n0,1000008,0,3,1050\n";
    let (out, invalid) = run(input);
    assert_eq!(
        out,
        "2,2,1025\n4,1000008,1\n3,1000005\n2,1,1025\n3,1000008\n4,1000007,4\n"
    );
    assert_eq!(invalid, 1);
}
