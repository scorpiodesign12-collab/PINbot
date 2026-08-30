-- Выполните этот запрос один раз в Supabase (в проекте peredatsha,
-- который уже используется другим вашим сайтом): Dashboard -> SQL Editor -> New query
--
-- Таблица названа с префиксом pinshare_, чтобы не пересекаться с
-- таблицами, которые уже использует peredatsha — они друг другу
-- никак не мешают, это просто ещё одна таблица в той же базе.

create table if not exists pinshare_connections (
    token text primary key,
    status text not null default 'pending',
    created_at double precision not null,
    tg_id bigint,
    username text,
    first_name text,
    photo_file_id text
);

create index if not exists pinshare_connections_tg_id_idx on pinshare_connections (tg_id);

-- RLS (Row Level Security) можно оставить включённым по умолчанию —
-- бэкенд обращается к базе через service_role key, который обходит RLS.
-- Если захотите разрешить чтение статуса напрямую с клиента без бэкенда,
-- потребуется отдельная политика — но в этой схеме это не нужно,
-- всё идёт через /api/* эндпоинты.
alter table pinshare_connections enable row level security;
