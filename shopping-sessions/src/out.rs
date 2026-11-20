//! Parquet output for both tables.

use crate::keys::unpack;
use crate::URow;
use parquet::basic::Encoding;
use parquet::data_type::{ByteArray, ByteArrayType, Int32Type, Int64Type};
use parquet::file::properties::{EnabledStatistics, WriterProperties, WriterVersion};
use parquet::file::writer::SerializedFileWriter;
use parquet::schema::parser::parse_message_type;
use std::fs::File;
use std::io::BufWriter;
use std::path::Path;
use std::sync::Arc;

const USER_SCHEMA: &str = "message user_daily {
  required binary user_id (STRING);
  required int32 day (DATE);
  required int64 sessions;
  required int64 events;
  required int64 revenue_cents;
  optional binary top_category (STRING);
}";

const CAMP_SCHEMA: &str = "message campaign_daily {
  required binary campaign_id (STRING);
  required binary channel (STRING);
  required int32 day (DATE);
  required int64 sessions;
  required int64 purchasing_sessions;
  required int64 revenue_cents;
  required int64 assisted_sessions;
}";

#[derive(Default)]
pub struct CampRows {
    names: Vec<ByteArray>,
    channels: Vec<ByteArray>,
    days: Vec<i32>,
    vals: [Vec<i64>; 4],
}

impl CampRows {
    pub fn push(&mut self, name: &[u8], channel: &[u8], day: i32, v: [i64; 4]) {
        self.names.push(ByteArray::from(name.to_vec()));
        self.channels.push(ByteArray::from(channel.to_vec()));
        self.days.push(day);
        for i in 0..4 {
            self.vals[i].push(v[i]);
        }
    }
}

fn props() -> Arc<WriterProperties> {
    Arc::new(
        WriterProperties::builder()
            .set_writer_version(WriterVersion::PARQUET_1_0)
            .set_dictionary_enabled(false)
            .set_encoding(Encoding::PLAIN)
            .set_statistics_enabled(EnabledStatistics::None)
            .set_max_row_group_size(1 << 30)
            .set_data_page_size_limit(1 << 20)
            .build(),
    )
}

fn write_user(path: &Path, rows: &[URow], cats: &[ByteArray]) {
    let schema = Arc::new(parse_message_type(USER_SCHEMA).unwrap());
    let file = BufWriter::with_capacity(1 << 20, File::create(path).unwrap());
    let mut w = SerializedFileWriter::new(file, schema, props()).unwrap();
    let mut rg = w.next_row_group().unwrap();
    let mut col = 0;
    while let Some(mut c) = rg.next_column().unwrap() {
        match col {
            0 => {
                let v: Vec<ByteArray> = rows.iter().map(|r| ByteArray::from(unpack(r.person))).collect();
                c.typed::<ByteArrayType>().write_batch(&v, None, None).unwrap();
            }
            1 => {
                let v: Vec<i32> = rows.iter().map(|r| r.day).collect();
                c.typed::<Int32Type>().write_batch(&v, None, None).unwrap();
            }
            2 | 3 | 4 => {
                let v: Vec<i64> = rows
                    .iter()
                    .map(|r| match col {
                        2 => r.sessions as i64,
                        3 => r.events as i64,
                        _ => r.revenue,
                    })
                    .collect();
                c.typed::<Int64Type>().write_batch(&v, None, None).unwrap();
            }
            _ => {
                let def: Vec<i16> = rows.iter().map(|r| (r.top >= 0) as i16).collect();
                let v: Vec<ByteArray> = rows.iter().filter(|r| r.top >= 0).map(|r| cats[r.top as usize].clone()).collect();
                c.typed::<ByteArrayType>().write_batch(&v, Some(&def), None).unwrap();
            }
        }
        c.close().unwrap();
        col += 1;
    }
    rg.close().unwrap();
    w.close().unwrap();
}

fn write_camp(path: &Path, rows: &CampRows) {
    let schema = Arc::new(parse_message_type(CAMP_SCHEMA).unwrap());
    let file = BufWriter::new(File::create(path).unwrap());
    let mut w = SerializedFileWriter::new(file, schema, props()).unwrap();
    let mut rg = w.next_row_group().unwrap();
    let mut col = 0;
    while let Some(mut c) = rg.next_column().unwrap() {
        match col {
            0 => {
                c.typed::<ByteArrayType>().write_batch(&rows.names, None, None).unwrap();
            }
            1 => {
                c.typed::<ByteArrayType>().write_batch(&rows.channels, None, None).unwrap();
            }
            2 => {
                c.typed::<Int32Type>().write_batch(&rows.days, None, None).unwrap();
            }
            k => {
                c.typed::<Int64Type>().write_batch(&rows.vals[k - 3], None, None).unwrap();
            }
        }
        c.close().unwrap();
        col += 1;
    }
    rg.close().unwrap();
    w.close().unwrap();
}

pub fn write_all(out: &Path, user_parts: Vec<Vec<URow>>, camp: CampRows, cats: &[Vec<u8>]) {
    let ud = out.join("user_daily");
    let cd = out.join("campaign_daily");
    std::fs::create_dir_all(&ud).unwrap();
    std::fs::create_dir_all(&cd).unwrap();
    let cats: Vec<ByteArray> = cats.iter().map(|c| ByteArray::from(c.clone())).collect();
    std::thread::scope(|s| {
        for (i, part) in user_parts.iter().enumerate() {
            let path = ud.join(format!("part-{i:05}.parquet"));
            let cats = &cats;
            s.spawn(move || write_user(&path, part, cats));
        }
        let path = cd.join("part-00000.parquet");
        let camp = &camp;
        s.spawn(move || write_camp(&path, camp));
    });
}
