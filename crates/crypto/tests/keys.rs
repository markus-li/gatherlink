use gatherlink_crypto::keys::{
    generate_ed25519_keypair, generate_x25519_keypair, node_id_from_ed25519_public, sign_document, verify_document,
    x25519_shared_secret, NODE_ID_LEN,
};

#[test]
fn signs_and_verifies_domain_separated_documents() {
    let (private_key, public_key) = generate_ed25519_keypair();
    let body = b"canonical";
    let signature = sign_document(&private_key, b"GATHERLINK_TEST_V1", body);

    assert!(verify_document(&public_key, b"GATHERLINK_TEST_V1", body, &signature).is_ok());
    assert!(verify_document(&public_key, b"GATHERLINK_OTHER_V1", body, &signature).is_err());
    assert_eq!(node_id_from_ed25519_public(&public_key).len(), NODE_ID_LEN);
}

#[test]
fn derives_matching_x25519_shared_secret() {
    let (a_private, a_public) = generate_x25519_keypair();
    let (b_private, b_public) = generate_x25519_keypair();

    assert_eq!(
        x25519_shared_secret(&a_private, &b_public),
        x25519_shared_secret(&b_private, &a_public)
    );
}
