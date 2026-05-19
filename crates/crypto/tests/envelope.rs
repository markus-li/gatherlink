use gatherlink_crypto::envelope::{
    decrypt_packet_without_replay, encrypt_frame_with_counter, TransportKeys, ENCRYPTED_DATA_HEADER_LEN,
    PACKET_TYPE_ENCRYPTED_DATA_V1,
};

#[test]
fn encrypted_data_packet_round_trips_frame_bytes() {
    let key = [7u8; 32];
    let packet = encrypt_frame_with_counter(55, &key, 9, b"frame").unwrap();

    assert_eq!(packet[0], PACKET_TYPE_ENCRYPTED_DATA_V1);
    assert_eq!(packet.len(), ENCRYPTED_DATA_HEADER_LEN + 5 + 16);
    let decrypted = decrypt_packet_without_replay(&key, &packet).unwrap();
    assert_eq!(decrypted.receiver_index, 55);
    assert_eq!(decrypted.counter, 9);
    assert_eq!(decrypted.plaintext, b"frame");
}

#[test]
fn invalid_packets_are_silent_drop_errors() {
    let key = [7u8; 32];
    let mut packet = encrypt_frame_with_counter(55, &key, 9, b"frame").unwrap();
    packet[20] ^= 1;

    assert!(decrypt_packet_without_replay(&key, &packet).is_err());
    assert!(decrypt_packet_without_replay(&key, b"short").is_err());
}

#[test]
fn transport_keys_reject_replayed_counter() {
    let client_to_server = [1u8; 32];
    let server_to_client = [2u8; 32];
    let mut client = TransportKeys::new(7, client_to_server, server_to_client);
    let mut server = TransportKeys::new(7, server_to_client, client_to_server);

    let packet = client.encrypt_frame(b"frame").unwrap();
    assert_eq!(server.decrypt_packet(&packet).unwrap().plaintext, b"frame");
    assert!(server.decrypt_packet(&packet).is_err());
}
