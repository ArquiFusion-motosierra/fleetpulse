-- FleetPulse OCI - 04_oauth.sql (T10). Ejecutar como FLEET despues de 03.
-- Cliente OAuth2 (client credentials) que usan las Lambdas de AWS. El client_id y el client_secret
-- van SOLO a Secrets Manager (fleetpulse/oci/ords) con scripts/push-secrets.sh. Nunca al repo ni al chat.
-- Si la consulta final muestra client_secret vacio (ORDS 23.3+ ya no lo expone), sacarlo en
-- Database Actions > REST > Security > OAuth Clients > fp_aws_writer.
-- Probar: curl -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials <base_url>oauth/token

WHENEVER SQLERROR EXIT SQL.SQLCODE

BEGIN
  OAUTH.DELETE_CLIENT(p_name => 'fp_aws_writer');
EXCEPTION
  WHEN OTHERS THEN NULL;  -- primera vez: no existe
END;
/

BEGIN
  OAUTH.CREATE_CLIENT(
    p_name            => 'fp_aws_writer',
    p_grant_type      => 'client_credentials',
    p_owner           => 'FleetPulse',
    p_description     => 'AWS oci-writer y snapshot-publisher',
    p_support_email   => 'fleetpulse@example.com',
    p_privilege_names => 'fp.writer');
  OAUTH.GRANT_CLIENT_ROLE(p_client_name => 'fp_aws_writer', p_role_name => 'fp_writer');
  COMMIT;
END;
/

SELECT name, client_id, client_secret FROM user_ords_clients WHERE name = 'fp_aws_writer';
