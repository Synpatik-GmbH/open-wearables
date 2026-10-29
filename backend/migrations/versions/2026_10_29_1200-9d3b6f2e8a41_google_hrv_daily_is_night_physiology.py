"""FORK (Calibra 2.72.2): Google Health's daily HRV value is night physiology for wearable history.

``google_hrv_rmssd_daily`` is stamped at 00:00 UTC of Google's date, the morning the night ends.
Like the RMSSD series it used to share, it is admitted by the recorded night that contains it, not
by its Berlin calendar date. The two functions are redefined with the code added to their
night-metric list and are otherwise unchanged; ``NIGHT_PHYSIOLOGY`` in
``wearable_history_ingress_repository.py`` carries the same list for the Python path.

Revision ID: 9d3b6f2e8a41
Revises: 7c2f4e9a1b30
Create Date: 2026-10-29 12:00:00
"""

from alembic import op

revision = "9d3b6f2e8a41"
down_revision = "7c2f4e9a1b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION wh_sample_admitted(record_source uuid, metric_id integer, instant timestamptz,
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
                'garmin_hrv_rmssd_nightly', 'google_hrv_rmssd_daily',
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
            END IF;
            RETURN calendar >= boundary.start_date;
        END $$;

        CREATE OR REPLACE FUNCTION wh_read_sample(record_source uuid, type_id integer, instant timestamptz)
            RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; metric text; night date; found_interval boolean := false;
            admitted_interval boolean := false;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=type_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN false; END IF;
            IF metric IN ('heart_rate','heart_rate_variability_sdnn','heart_rate_variability_rmssd',
                'garmin_hrv_rmssd_nightly', 'google_hrv_rmssd_daily',
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
    """)


def downgrade() -> None:
    op.execute(r"""
        CREATE OR REPLACE FUNCTION wh_sample_admitted(record_source uuid, metric_id integer, instant timestamptz,
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
                'garmin_hrv_rmssd_nightly',
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
            END IF;
            RETURN calendar >= boundary.start_date;
        END $$;

        CREATE OR REPLACE FUNCTION wh_read_sample(record_source uuid, type_id integer, instant timestamptz)
            RETURNS boolean
        LANGUAGE plpgsql STABLE AS $$
        DECLARE source_row data_source; metric text; night date; found_interval boolean := false;
            admitted_interval boolean := false;
        BEGIN
            SELECT code INTO metric FROM series_type_definition WHERE id=type_id;
            IF metric='weight' THEN RETURN true; END IF;
            SELECT * INTO source_row FROM data_source WHERE id=record_source;
            IF NOT FOUND THEN RETURN false; END IF;
            IF metric IN ('heart_rate','heart_rate_variability_sdnn','heart_rate_variability_rmssd',
                'garmin_hrv_rmssd_nightly',
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
    """)
