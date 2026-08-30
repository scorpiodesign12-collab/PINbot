-- Выполните этот запрос один раз в Supabase: Dashboard -> SQL Editor -> New query

create table if not exists connections (
    token text primary key,
    status text not null default 'pending',
    created_at double precision not null,
    tg_id bigint,
    username text,
    first_name text,
    photo_file_id text
);

create index if not exists connections_tg_id_idx on connections (tg_id);

-- RLS (Row Level Security) можно оставить включённым по умолчанию —
-- бэкенд обращается к базе через service_role key, который обходит RLS.
-- Если захотите разрешить чтение статуса напрямую с клиента без бэкенда,
-- потребуется отдельная политика — но в этой схеме это не нужно,
-- всё идёт через /api/* эндпоинты.
alter table connections enable row level security;
