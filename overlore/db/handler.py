from __future__ import annotations

import json
import os
from sqlite3 import Connection
from threading import Lock
from typing import Any, cast

import sqlean

from overlore.eternum.constants import Realms
from overlore.eternum.types import AttackingEntityIds, RealmPosition, ResourceAmounts
from overlore.graphql.constants import EventType as EventKeyHash
from overlore.sqlite.constants import EventType as SqLiteEventType
from overlore.sqlite.types import StoredEvent
from overlore.types import EventData, EventKeys, ParsedEvent, ToriiEvent


class DatabaseHandler:
    path: str
    db: Connection
    _instance = None
    realms: Realms

    # create a lock
    lock = Lock()

    def __lock(self) -> None:
        self.lock.acquire(blocking=True, timeout=1000)

    def __release(self) -> None:
        self.lock.release()

    @classmethod
    def instance(cls) -> DatabaseHandler:
        if cls._instance is None:
            print("Creating db interface")
            cls._instance = cls.__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        raise RuntimeError("Call instance() instead")

    def __init_db(self) -> None:
        self.db.execute("SELECT InitSpatialMetaData(1);")

    def __load_sqlean(self, path: str) -> Connection:
        conn: Connection = sqlean.connect(path)
        # TODO Do we have to load the extension everytime we start up or only once when the db is first created?
        conn.enable_load_extension(True)
        conn.execute('SELECT load_extension("mod_spatialite")')
        return conn

    def __use_initial_queries(self) -> None:
        # Event db definition
        # -> Event type
        # -> pos_1 (maker/attacker)
        # -> pos_2 (taker/target)
        # -> timestamp
        # -> importance
        # -> metadata ()
        self.db.execute(
            """
                            CREATE TABLE IF NOT EXISTS events (
                                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                                type INTEGER NOT NULL,
                                importance INTEGER NOT NULL,
                                ts INTEGER NOT NULL,
                                metadata TEXT
                            );
                        """
        )
        # maker/attacker
        self.db.execute("""SELECT AddGeometryColumn('events', 'active_pos', 0, 'POINT', 'XY', 1);""")
        # taker/target
        self.db.execute("""SELECT AddGeometryColumn('events', 'passive_pos', 0, 'POINT', 'XY', 1);""")

    def __insert(self, query: str, values: tuple[Any]) -> int:
        # lock mutex
        self.__lock()
        cursor = self.db.cursor()
        cursor.execute(query, values)
        self.db.commit()
        added_id = cursor.lastrowid if cursor.lastrowid else 0
        # unlock mutex
        self.__release()
        return added_id

    def __add(self, obj: ParsedEvent) -> int:
        event_type = obj["type"]
        del obj["type"]
        active_pos = cast(RealmPosition, obj["active_pos"])
        del obj["active_pos"]
        passive_pos = cast(RealmPosition, obj["passive_pos"])
        del obj["passive_pos"]
        ts = obj["ts"]
        del obj["ts"]
        obj["importance"]
        del obj["importance"]
        additional_data = json.dumps(obj)
        query = (
            "INSERT INTO events (type, importance, ts, metadata, active_pos, passive_pos) VALUES (?, ?,"
            " ?, ?, MakePoint(?,?),MakePoint(?, ?));"
        )
        values: tuple[Any, ...] = (
            event_type,
            4,
            ts,
            additional_data,
            active_pos[0],
            active_pos[1],
            passive_pos[0],
            passive_pos[1],
        )
        added_id = self.__insert(query, values)
        return added_id

    def __get_event_type(self, keys: EventKeys) -> str:
        return str(keys[0])

    def __parse_resources(self, data: list[str]) -> tuple[list[str], ResourceAmounts]:
        resource_len = int(data[0], base=16)
        end_idx_resources = resource_len * 2
        resources = [
            {"type": int(data[i], base=16), "amount": int(int(data[i + 1], base=16) / 1000)}
            for i in range(1, end_idx_resources, 2)
        ]
        return (data[end_idx_resources + 1 :], resources)

    def __parse_attacking_entity_ids(self, data: list[str]) -> tuple[list[str], AttackingEntityIds]:
        length = int(data[0], base=16)
        attacking_entity_ids = [int(data[i], base=16) for i in range(1, length)]
        return (data[1 + length :], attacking_entity_ids)

    def __parse_combat_outcome_event(self, keys: EventKeys, data: EventData) -> ParsedEvent:
        attacker_realm_id = int(keys[1], base=16)
        target_realm_entity_id = int(keys[2], base=16)

        (data, attacking_entity_ids) = self.__parse_attacking_entity_ids(data)
        (data, stolen_resources) = self.__parse_resources(data)

        winner = int(data[0], base=16)
        damage = int(data[1], base=16)
        ts = int(data[2], base=16)

        parsed_event: ParsedEvent = {
            "type": SqLiteEventType.COMBAT_OUTCOME.value,
            "active_pos": self.realms.position_by_id(attacker_realm_id),
            "passive_pos": self.realms.position_by_id(target_realm_entity_id),
            "attacking_entity_ids": attacking_entity_ids,
            "stolen_resources": stolen_resources,
            "winner": winner,
            "damage": damage,
            "importance": 0,
            "ts": ts,
        }
        return parsed_event

    def __parse_trade_event(self, keys: EventKeys, data: EventData) -> ParsedEvent:
        _trade_id = int(keys[1], base=16)
        maker_id = int(data[0], base=16)
        taker_id = int(data[1], base=16)

        data = data[2:]

        (data, resources_maker) = self.__parse_resources(data)
        (data, resources_taker) = self.__parse_resources(data)
        ts = int(data[0], base=16)

        parsed_event: ParsedEvent = {
            "type": SqLiteEventType.ORDER_ACCEPTED.value,
            "active_pos": self.realms.position_by_id(maker_id),
            "passive_pos": self.realms.position_by_id(taker_id),
            "resources_maker": resources_maker,
            "resources_taker": resources_taker,
            "importance": 0,
            "ts": ts,
        }
        return parsed_event

    def init(self, path: str = "./events.db") -> DatabaseHandler:
        db_first_launch = not os.path.exists(path)
        self.db = self.__load_sqlean(path)
        if db_first_launch:
            self.__init_db()
            self.__use_initial_queries()

        self.realms = Realms.instance().init()
        return self

    def get_by_id(self, event_id: int) -> StoredEvent:
        # lock mutex
        self.__lock()
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT id, type, importance, ts, metadata, X(active_pos), Y(active_pos), X(passive_pos), Y(passive_pos)"
            " FROM events WHERE id=?",
            (event_id,),
        )
        record = cursor.fetchall()
        # unlock mutex
        self.__release()
        record = list(record[0])
        record = record[:5] + [(record[5], record[6]), (record[7], record[8])]
        return record

    def get_all(self) -> list[StoredEvent]:
        # lock mutex
        self.__lock()
        query = """SELECT id, type, importance, ts, metadata, X(active_pos), Y(active_pos), X(passive_pos), Y(passive_pos) from events"""
        cursor = self.db.cursor()
        cursor.execute(query)
        records = cursor.fetchall()
        # unlock mutex
        self.__release()
        records = [[*list(tup[:5]), (tup[5], tup[6]), (tup[7], tup[8])] for tup in records]
        return records

    def process_event(self, event: ToriiEvent) -> int:
        event_emitted = event.get("eventEmitted")
        if not event_emitted:
            raise RuntimeError("eventEmitted no present in event")
        keys = cast(EventKeys, event_emitted.get("keys"))
        if not keys:
            raise RuntimeError("Event had no keys")
        data = cast(EventData, event_emitted.get("data"))
        if not data:
            raise RuntimeError("Event had no data")
        if self.__get_event_type(keys=keys) == EventKeyHash.COMBAT_OUTCOME.value:
            parsed_event = self.__parse_combat_outcome_event(keys=keys, data=data)
        else:
            parsed_event = self.__parse_trade_event(keys=keys, data=data)
        return self.__add(parsed_event)
