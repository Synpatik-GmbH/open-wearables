"""2.96.9.1 v0.4 first-connection history authority; no existing-account backfill.

Physical S12 purge is a separate release dependency, not completed by this migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a29691004b01"
down_revision = "635d0674389f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_point_series_archive", sa.Column("history_start_date", sa.Date(), nullable=True))
    op.create_table(
        "wearable_history_sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("connection_day", sa.Date(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("freeze_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("night_dates", postgresql.ARRAY(sa.Date()), server_default=sa.text("'{}'::date[]"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_wearable_history_user_provider"),
        sa.CheckConstraint(
            "start_date BETWEEN connection_day - 28 AND connection_day", name="ck_wearable_history_window"
        ),
    )
    op.execute(r"""
        CREATE FUNCTION wh_night_day(start_at timestamptz, end_at timestamptz, zone text) RETURNS date
        LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE local_at timestamp; magnitude interval;
        BEGIN
            IF start_at IS NULL OR end_at IS NULL OR end_at <= start_at THEN RETURN NULL; END IF;
            local_at := start_at AT TIME ZONE 'Europe/Berlin';
            IF zone ~ '^[+-][0-9]{2}:[0-9]{2}$' AND substring(zone,5,2)::int < 60 THEN
                magnitude := make_interval(hours=>substring(zone,2,2)::int, mins=>substring(zone,5,2)::int);
                IF magnitude <= interval '14 hours' THEN
                    local_at := (start_at AT TIME ZONE 'UTC') + CASE WHEN left(zone,1)='-' THEN -magnitude ELSE
            magnitude END;
                END IF;
            END IF;
            IF local_at::time >= time '18:00' THEN RETURN local_at::date + 1; END IF;
            IF local_at::time < time '06:00' THEN RETURN local_at::date; END IF;
            RETURN NULL;
        END $$;

        CREATE FUNCTION wh_read_date(member uuid, source_provider text, day date) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT CASE WHEN EXISTS (SELECT 1 FROM wearable_history_sources WHERE user_id=member)
                THEN EXISTS (SELECT 1 FROM wearable_history_sources
                    WHERE user_id=member AND provider=source_provider AND day >= start_date)
                ELSE true END
        $$;

        CREATE FUNCTION wh_read_event(record_source uuid, category text, start_at timestamptz,
            end_at timestamptz, zone text) RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; day date;
        BEGIN
            IF category NOT IN ('sleep','workout') THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN false; END IF;
            day := (start_at AT TIME ZONE 'Europe/Berlin')::date;
            IF category='sleep' THEN day := coalesce(wh_night_day(start_at,end_at,zone),day); END IF;
            RETURN wh_read_date(source_row.user_id,source_row.provider::text,day);
        END $$;

        CREATE FUNCTION wh_read_sample(record_source uuid, type_id integer, instant timestamptz) RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; metric text; night date; found_interval boolean := false;
            admitted_interval boolean := false;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=type_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN false; END IF;
            IF metric IN ('heart_rate','heart_rate_variability_sdnn','heart_rate_variability_rmssd',
                'resting_heart_rate','respiratory_rate','oxygen_saturation','skin_temperature','body_temperature') THEN
                FOR night IN SELECT wh_night_day(e.start_datetime,e.end_datetime,e.zone_offset)
                    FROM event_record e JOIN data_source ds ON ds.id=e.data_source_id
                    WHERE ds.user_id=source_row.user_id AND ds.provider=source_row.provider AND e.category='sleep'
                      AND e.start_datetime <= instant AND e.end_datetime >= instant LOOP
                    IF night IS NOT NULL THEN
                        found_interval := true;
                        admitted_interval := admitted_interval OR
                            wh_read_date(source_row.user_id,source_row.provider::text,night);
                    END IF;
                END LOOP;
                IF found_interval THEN RETURN admitted_interval; END IF;
            END IF;
            RETURN wh_read_date(source_row.user_id,source_row.provider::text,
                (instant AT TIME ZONE 'Europe/Berlin')::date);
        END $$;

        CREATE FUNCTION wh_read_score(member uuid, record_source uuid, source_provider text,
            parent_id uuid, instant timestamptz) RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; parent event_record;
        BEGIN
            IF parent_id IS NOT NULL THEN
                SELECT * INTO parent FROM event_record WHERE id=parent_id;
                IF FOUND THEN RETURN wh_read_event(parent.data_source_id,parent.category,
                    parent.start_datetime,parent.end_datetime,parent.zone_offset); END IF;
                RETURN false;
            END IF;
            IF record_source IS NOT NULL THEN
                SELECT * INTO source_row FROM data_source WHERE id=record_source;
                IF NOT FOUND THEN RETURN false; END IF;
                source_provider := source_row.provider::text;
            END IF;
            IF source_provider='internal' THEN RETURN true; END IF;
            RETURN wh_read_date(member,source_provider,(instant AT TIME ZONE 'Europe/Berlin')::date);
        END $$;

        CREATE FUNCTION wh_archive_ready(record_source uuid, type_id integer, instant timestamptz) RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE source_row data_source; boundary wearable_history_sources; metric text;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=type_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            SELECT * INTO boundary FROM wearable_history_sources
                WHERE user_id=source_row.user_id AND provider=source_row.provider::text;
            IF FOUND AND clock_timestamp() < boundary.freeze_at THEN RETURN false; END IF;
            RETURN wh_read_sample(record_source,type_id,instant);
        END $$;

        CREATE FUNCTION wh_read_archive(record_source uuid, type_id integer, instant timestamptz,
            proved_start date) RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; boundary wearable_history_sources; metric text;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=type_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            SELECT * INTO boundary FROM wearable_history_sources
                WHERE user_id=source_row.user_id AND provider=source_row.provider::text;
            IF FOUND AND proved_start=boundary.start_date THEN RETURN true; END IF;
            RETURN wh_read_date(source_row.user_id,source_row.provider::text,
                (instant AT TIME ZONE 'Europe/Berlin')::date);
        END $$;

        CREATE FUNCTION wh_date_admitted(member uuid, source_provider text, day date) RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE boundary wearable_history_sources;
        BEGIN
            SELECT * INTO boundary FROM wearable_history_sources
                WHERE user_id=member AND provider=source_provider FOR UPDATE;
            IF FOUND THEN RETURN clock_timestamp() < boundary.freeze_at OR day >= boundary.start_date; END IF;
            IF EXISTS (SELECT 1 FROM user_connection WHERE user_id=member AND provider::text=source_provider)
                OR EXISTS (SELECT 1 FROM wearable_history_sources WHERE user_id=member) THEN
                RAISE EXCEPTION 'Wearable source history unavailable' USING ERRCODE='23514';
            END IF;
            RETURN true;
        END $$;

        CREATE FUNCTION wh_event_admitted(record_source uuid, category text, start_at timestamptz,
            end_at timestamptz, zone text) RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE source_row data_source; day date;
        BEGIN
            IF category NOT IN ('sleep','workout') THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN true; END IF;
            day := (start_at AT TIME ZONE 'Europe/Berlin')::date;
            IF category='sleep' THEN day := COALESCE(wh_night_day(start_at,end_at,zone),day); END IF;
            RETURN wh_date_admitted(source_row.user_id,source_row.provider::text,day);
        END $$;

        CREATE FUNCTION wh_sample_admitted(record_source uuid, metric_id integer, instant timestamptz,
            sample_zone text DEFAULT NULL) RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE source_row data_source; boundary wearable_history_sources; metric text; calendar date;
            night date; found_interval boolean := false; admitted_interval boolean := false;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=metric_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN true; END IF;
            SELECT * INTO boundary FROM wearable_history_sources
                WHERE user_id=source_row.user_id AND provider=source_row.provider::text FOR UPDATE;
            calendar := (instant AT TIME ZONE 'Europe/Berlin')::date;
            IF NOT FOUND THEN RETURN wh_date_admitted(source_row.user_id,source_row.provider::text,calendar); END IF;
            IF clock_timestamp() < boundary.freeze_at THEN RETURN true; END IF;
            IF metric IN ('heart_rate','heart_rate_variability_sdnn','heart_rate_variability_rmssd',
                'resting_heart_rate','respiratory_rate','oxygen_saturation','skin_temperature','body_temperature') THEN
                FOR night IN SELECT wh_night_day(e.start_datetime,e.end_datetime,e.zone_offset)
                    FROM event_record e JOIN data_source ds ON ds.id=e.data_source_id
                    WHERE ds.user_id=source_row.user_id AND ds.provider=source_row.provider AND e.category='sleep'
                      AND e.start_datetime <= instant AND e.end_datetime >= instant LOOP
                    IF night IS NOT NULL THEN
                        found_interval := true;
                        admitted_interval := admitted_interval OR night >= boundary.start_date;
                    END IF;
                END LOOP;
                IF found_interval THEN RETURN admitted_interval; END IF;
                IF calendar=boundary.start_date-1
                    AND wh_night_day(instant,instant+interval '1 microsecond',sample_zone)=boundary.start_date THEN
                    RAISE EXCEPTION 'First-night association unavailable' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN calendar >= boundary.start_date;
        END $$;

        CREATE FUNCTION wh_parent_admitted(parent_id uuid) RETURNS boolean
        LANGUAGE plpgsql AS $$
        DECLARE parent event_record;
        BEGIN
            SELECT * INTO parent FROM event_record WHERE id=parent_id;
            IF NOT FOUND THEN RETURN true; END IF;
            RETURN wh_event_admitted(parent.data_source_id,parent.category,
                parent.start_datetime,parent.end_datetime,parent.zone_offset);
        END $$;

        CREATE FUNCTION wh_guard_ingress() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE admitted boolean := true; source_row data_source; sample data_point_series; boundary
            wearable_history_sources; has_contributors boolean := false; metric text;
        BEGIN
            IF TG_TABLE_NAME='event_record' THEN
                admitted :=
            wh_event_admitted(NEW.data_source_id,NEW.category,NEW.start_datetime,NEW.end_datetime,NEW.zone_offset);
            ELSIF TG_TABLE_NAME='data_point_series' THEN
                admitted := wh_sample_admitted(NEW.data_source_id,NEW.series_type_definition_id,NEW.recorded_at,
                    NEW.zone_offset);
            ELSIF TG_TABLE_NAME IN ('sleep_details','workout_details') THEN
                admitted := wh_parent_admitted(NEW.record_id);
            ELSIF TG_TABLE_NAME='health_score' THEN
                IF NEW.event_record_id IS NOT NULL THEN admitted := wh_parent_admitted(NEW.event_record_id);
                ELSIF NEW.data_source_id IS NOT NULL THEN
                    SELECT * INTO source_row FROM data_source WHERE id=NEW.data_source_id;
                    admitted := wh_date_admitted(NEW.user_id,source_row.provider::text,(NEW.recorded_at AT TIME
            ZONE 'Europe/Berlin')::date);
                ELSIF NEW.provider::text='internal' THEN admitted := true;
                ELSE admitted := wh_date_admitted(NEW.user_id,NEW.provider::text,(NEW.recorded_at AT TIME ZONE
            'Europe/Berlin')::date);
                END IF;
            ELSIF TG_TABLE_NAME='data_point_series_archive' THEN
                -- A UTC bucket can precede the source date while containing admitted Berlin
                -- observations or the first night's evening. Prove every contributor before
                -- the archiver deletes raw rows; an unproved pre-start bucket is refused.
                SELECT code INTO metric FROM series_type_definition WHERE id=NEW.series_type_definition_id;
                SELECT * INTO source_row FROM data_source WHERE id=NEW.data_source_id;
                SELECT * INTO boundary FROM wearable_history_sources
                    WHERE user_id=source_row.user_id AND provider=source_row.provider::text FOR UPDATE;
                NEW.history_start_date := NULL;
                IF FOUND AND metric <> 'weight' AND clock_timestamp() < boundary.freeze_at THEN
                    RAISE EXCEPTION 'Provisional wearable samples must remain raw' USING ERRCODE='23514';
                END IF;
                admitted := wh_read_sample(NEW.data_source_id,NEW.series_type_definition_id,NEW.bucket_start_at);
                FOR sample IN SELECT * FROM data_point_series WHERE data_source_id=NEW.data_source_id
                    AND series_type_definition_id=NEW.series_type_definition_id
                    AND recorded_at >= NEW.bucket_start_at AND recorded_at < NEW.bucket_start_at+interval '1 day' LOOP
                    IF NOT
            wh_read_sample(sample.data_source_id,sample.series_type_definition_id,sample.recorded_at) THEN
                        RAISE EXCEPTION 'Archive contains pre-start data' USING ERRCODE='23514';
                    END IF;
                    has_contributors := true;
                    admitted := true;
                END LOOP;
                IF TG_OP='UPDATE' AND OLD.data_source_id=NEW.data_source_id
                    AND OLD.series_type_definition_id=NEW.series_type_definition_id
                    AND OLD.bucket_start_at=NEW.bucket_start_at AND OLD.history_start_date=boundary.start_date THEN
                    admitted := true;
                END IF;
                IF boundary.id IS NOT NULL AND metric <> 'weight' AND admitted THEN
                    IF has_contributors THEN NEW.history_start_date := boundary.start_date;
                    ELSIF TG_OP='UPDATE' AND OLD.data_source_id=NEW.data_source_id
                        AND OLD.series_type_definition_id=NEW.series_type_definition_id
                        AND OLD.bucket_start_at=NEW.bucket_start_at AND OLD.history_start_date=boundary.start_date THEN
                        NEW.history_start_date := OLD.history_start_date;
                    END IF;
                END IF;
            END IF;
            IF NOT admitted THEN RAISE EXCEPTION 'Frozen source rejects pre-start data' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
    """)
    for table in ("event_record", "data_point_series_archive"):
        op.execute(
            f"CREATE TRIGGER wh_ingress BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION wh_guard_ingress()"
        )
    for table in ("data_point_series", "sleep_details", "workout_details", "health_score"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER wh_ingress AFTER INSERT OR UPDATE ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION wh_guard_ingress()"
        )


def downgrade() -> None:
    for table in (
        "event_record",
        "data_point_series_archive",
        "data_point_series",
        "sleep_details",
        "workout_details",
        "health_score",
    ):
        op.execute(f"DROP TRIGGER wh_ingress ON {table}")
    op.execute(
        "DROP FUNCTION wh_guard_ingress(), wh_parent_admitted(uuid), "
        "wh_read_archive(uuid,integer,timestamptz,date), wh_archive_ready(uuid,integer,timestamptz), "
        "wh_read_score(uuid,uuid,text,uuid,timestamptz), wh_read_sample(uuid,integer,timestamptz), "
        "wh_read_event(uuid,text,timestamptz,timestamptz,text), wh_read_date(uuid,text,date), "
        "wh_sample_admitted(uuid,integer,timestamptz,text), wh_event_admitted(uuid,text,timestamptz,timestamptz,text), "
        "wh_date_admitted(uuid,text,date), wh_night_day(timestamptz,timestamptz,text)"
    )
    op.drop_table("wearable_history_sources")
    op.drop_column("data_point_series_archive", "history_start_date")
