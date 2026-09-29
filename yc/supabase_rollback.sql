-- Откат переезда: вернуть сохранение в Supabase (прежнее тело afisha_save до 29.09.2026).
-- Применять только вместе с возвратом YC_URL = "" в index.html и после того,
-- как свежие данные из Яндекса перенесены обратно.
CREATE OR REPLACE FUNCTION public.afisha_save(p_code text, p_data jsonb, p_since timestamp with time zone)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare
  v_role text;
  v_label text;
  v_cur timestamptz;
  v_by text;
  v_new timestamptz;
begin
  select a.role, a.label into v_role, v_label
  from public.afisha_access a where a.code = p_code;

  if v_role is null then
    return jsonb_build_object('ok', false, 'error', 'bad_code');
  end if;
  if v_role <> 'editor' then
    return jsonb_build_object('ok', false, 'error', 'read_only');
  end if;

  select updated_at, updated_by into v_cur, v_by
  from public.afisha_state where id = 'akusha' for update;

  if p_since is not null and v_cur > p_since then
    return jsonb_build_object(
      'ok', false, 'error', 'conflict',
      'updated_at', v_cur, 'updated_by', v_by,
      'data', (select data from public.afisha_state where id = 'akusha')
    );
  end if;

  update public.afisha_state
     set data = p_data, updated_at = now(), updated_by = v_label
   where id = 'akusha'
   returning updated_at into v_new;

  return jsonb_build_object('ok', true, 'updated_at', v_new, 'updated_by', v_label);
end $function$;
