//! The pair config file (D23): what B reads from it and what makes it refuse
//! to start.

use match_engine::config::{parse_pairs, Config};
use match_engine::types::SymbolSpec;
use serde_json::{json, Value};

fn file(pairs: Value) -> String {
    json!({ "version": 1, "pairs": pairs }).to_string()
}

fn pair(symbol: &str, tick: Value, lot: Value) -> Value {
    json!({ "symbol": symbol, "tick": tick, "lot": lot })
}

fn error(text: &str) -> String {
    format!("{:#}", parse_pairs(text).unwrap_err())
}

#[test]
fn reads_symbol_tick_and_lot_in_file_order_and_ignores_everything_else() {
    let text = json!({
        "version": 1,
        "assets": { "BTC": { "decimals": 8 } },
        "owner_note": "fields B does not know are fine",
        "pairs": [
            { "symbol": "ETH-USDT", "base": "ETH", "quote": "USDT",
              "price_decimals": 2, "qty_decimals": 6,
              "tick": 5, "lot": 1000, "min_qty": 1000, "max_notional": 1 },
            { "symbol": "BTC-USDT", "tick": 1, "lot": 100 }
        ]
    })
    .to_string();
    assert_eq!(
        parse_pairs(&text).unwrap(),
        vec![
            ("ETH-USDT".to_string(), SymbolSpec::new(5, 1000)),
            ("BTC-USDT".to_string(), SymbolSpec::new(1, 100)),
        ]
    );
}

#[test]
fn the_file_shipped_with_the_compose_snippet_is_valid() {
    let text = include_str!("../config/pairs.json");
    let names: Vec<String> = parse_pairs(text)
        .unwrap()
        .into_iter()
        .map(|p| p.0)
        .collect();
    assert_eq!(names, ["BTC-USDT", "ETH-USDT"]);
}

#[test]
fn only_version_1_is_understood() {
    let text = json!({ "version": 2, "pairs": [pair("A", json!(1), json!(1))] }).to_string();
    assert!(error(&text).contains("version 2"));
    let text = json!({ "pairs": [pair("A", json!(1), json!(1))] }).to_string();
    assert!(error(&text).contains("version"));
}

#[test]
fn tick_and_lot_must_be_positive_integers() {
    for (tick, lot) in [
        (json!(0), json!(1)),
        (json!(1), json!(-1)),
        (json!(0.5), json!(1)),
        (json!("1"), json!(1)),
        (json!(1), Value::Null),
    ] {
        let text = file(json!([pair("A", tick.clone(), lot.clone())]));
        assert!(parse_pairs(&text).is_err(), "tick {tick}, lot {lot}");
    }
    let missing = file(json!([{ "symbol": "A", "tick": 1 }]));
    assert!(error(&missing).contains("lot"));
}

#[test]
fn symbols_must_be_unique_non_empty_and_safe_in_a_path() {
    let twice = file(json!([
        pair("A", json!(1), json!(1)),
        pair("A", json!(2), json!(2))
    ]));
    assert!(error(&twice).contains("twice"));
    for bad in ["", "BTC/USDT", "BTC USDT", "../x"] {
        assert!(
            parse_pairs(&file(json!([pair(bad, json!(1), json!(1))]))).is_err(),
            "{bad:?}"
        );
    }
}

#[test]
fn at_least_one_pair_is_required() {
    assert!(error(&file(json!([]))).contains("no pairs"));
    assert!(parse_pairs("not json").is_err());
}

#[test]
fn the_path_comes_from_pairs_file_and_a_missing_file_names_it() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("pairs.json");
    std::fs::write(&path, file(json!([pair("A", json!(1), json!(1))]))).unwrap();
    let p = path.to_str().unwrap().to_string();
    let cfg = Config::from_lookup(|k| (k == "PAIRS_FILE").then(|| p.clone())).unwrap();
    assert_eq!(cfg.symbols, vec![("A".to_string(), SymbolSpec::UNIT)]);

    let gone = dir.path().join("gone.json").to_str().unwrap().to_string();
    let err = Config::from_lookup(|k| (k == "PAIRS_FILE").then(|| gone.clone())).unwrap_err();
    assert!(format!("{err:#}").contains("gone.json"));
}
