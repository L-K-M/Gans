/*
 * Generates the libsodium interop vectors in tests/vectors/libsodium.json by calling the
 * C library directly — independent of PyNaCl, so the Python wrappers in gans/crypto.py are
 * checked against libsodium itself (in particular crypto_kdf_derive_from_key, which PyNaCl
 * doesn't bind and which gans/crypto.py reconstructs from BLAKE2b salt/personal).
 *
 *   cc -o gen_vectors gen_vectors.c $(pkg-config --cflags --libs libsodium) && ./gen_vectors > ../vectors/libsodium.json
 *
 * Deterministic inputs are derived from fixed seeds; the sealed box necessarily uses a
 * random ephemeral key, so its ciphertext differs per run but always decrypts to the
 * same plaintext.
 */
#include <sodium.h>
#include <stdio.h>
#include <string.h>

static void hex(const char *label, const unsigned char *data, size_t len, int trailing_comma) {
    char *out = sodium_malloc(len * 2 + 1);
    sodium_bin2hex(out, len * 2 + 1, data, len);
    printf("  \"%s\": \"%s\"%s\n", label, out, trailing_comma ? "," : "");
    sodium_free(out);
}

static void fill(unsigned char *buf, size_t len, unsigned char seed) {
    for (size_t i = 0; i < len; i++) buf[i] = (unsigned char)((seed + i * 7) & 0xff);
}

int main(void) {
    if (sodium_init() < 0) return 1;
    printf("{\n");

    /* crypto_kdf_derive_from_key(id=1, ctx="loginctx") — Ente's login-key derivation. */
    unsigned char kek[crypto_kdf_KEYBYTES];
    fill(kek, sizeof kek, 0x11);
    unsigned char subkey[32];
    crypto_kdf_derive_from_key(subkey, sizeof subkey, 1, "loginctx", kek);
    hex("kdf_key", kek, sizeof kek, 1);
    hex("kdf_subkey_id1_loginctx_32", subkey, sizeof subkey, 1);
    unsigned char subkey2[32];
    crypto_kdf_derive_from_key(subkey2, sizeof subkey2, 2, "loginctx", kek);
    hex("kdf_subkey_id2_loginctx_32", subkey2, sizeof subkey2, 1);

    /* crypto_pwhash Argon2id13 with small parameters (memlimit in BYTES). */
    unsigned char salt[crypto_pwhash_SALTBYTES];
    fill(salt, sizeof salt, 0x22);
    unsigned char kek_out[32];
    const char *password = "correct horse battery staple";
    if (crypto_pwhash(kek_out, sizeof kek_out, password, strlen(password), salt,
                      3, 16 * 1024 * 1024, crypto_pwhash_ALG_ARGON2ID13) != 0) return 2;
    printf("  \"pwhash_password\": \"%s\",\n", password);
    hex("pwhash_salt", salt, sizeof salt, 1);
    printf("  \"pwhash_opslimit\": 3,\n  \"pwhash_memlimit\": %d,\n", 16 * 1024 * 1024);
    hex("pwhash_out", kek_out, sizeof kek_out, 1);

    /* crypto_secretbox_easy */
    unsigned char sb_key[crypto_secretbox_KEYBYTES], sb_nonce[crypto_secretbox_NONCEBYTES];
    fill(sb_key, sizeof sb_key, 0x33);
    fill(sb_nonce, sizeof sb_nonce, 0x44);
    const unsigned char sb_msg[32] = "master-key-material-0123456789ab";
    unsigned char sb_ct[32 + crypto_secretbox_MACBYTES];
    crypto_secretbox_easy(sb_ct, sb_msg, 32, sb_nonce, sb_key);
    hex("secretbox_key", sb_key, sizeof sb_key, 1);
    hex("secretbox_nonce", sb_nonce, sizeof sb_nonce, 1);
    hex("secretbox_plaintext", sb_msg, 32, 1);
    hex("secretbox_ciphertext", sb_ct, sizeof sb_ct, 1);

    /* crypto_box_seal (ephemeral → random ciphertext) */
    unsigned char pk[crypto_box_PUBLICKEYBYTES], sk[crypto_box_SECRETKEYBYTES], seed[crypto_box_SEEDBYTES];
    fill(seed, sizeof seed, 0x55);
    crypto_box_seed_keypair(pk, sk, seed);
    const unsigned char token[24] = "sealed-token-0123456789";
    unsigned char sealed[24 + crypto_box_SEALBYTES];
    crypto_box_seal(sealed, token, 24, pk);
    hex("sealedbox_pk", pk, sizeof pk, 1);
    hex("sealedbox_sk", sk, sizeof sk, 1);
    hex("sealedbox_plaintext", token, 24, 1);
    hex("sealedbox_ciphertext", sealed, sizeof sealed, 1);

    /* secretstream, single chunk, once with TAG_MESSAGE (what Ente Auth writes) and once
       with TAG_FINAL. Header is random per push; both are recorded. */
    unsigned char ss_key[crypto_secretstream_xchacha20poly1305_KEYBYTES];
    fill(ss_key, sizeof ss_key, 0x66);
    const char *uri = "\"otpauth://totp/GitHub:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=GitHub\"";
    size_t uri_len = strlen(uri);
    unsigned char header[crypto_secretstream_xchacha20poly1305_HEADERBYTES];
    unsigned char *ct = sodium_malloc(uri_len + crypto_secretstream_xchacha20poly1305_ABYTES);
    crypto_secretstream_xchacha20poly1305_state st;
    unsigned long long ct_len;

    crypto_secretstream_xchacha20poly1305_init_push(&st, header, ss_key);
    crypto_secretstream_xchacha20poly1305_push(&st, ct, &ct_len, (const unsigned char *)uri, uri_len, NULL, 0,
                                               crypto_secretstream_xchacha20poly1305_TAG_MESSAGE);
    hex("secretstream_key", ss_key, sizeof ss_key, 1);
    printf("  \"secretstream_plaintext\": %s,\n", "\"\\\"otpauth://totp/GitHub:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=GitHub\\\"\"");
    hex("secretstream_message_tag_header", header, sizeof header, 1);
    hex("secretstream_message_tag_ciphertext", ct, (size_t)ct_len, 1);

    crypto_secretstream_xchacha20poly1305_init_push(&st, header, ss_key);
    crypto_secretstream_xchacha20poly1305_push(&st, ct, &ct_len, (const unsigned char *)uri, uri_len, NULL, 0,
                                               crypto_secretstream_xchacha20poly1305_TAG_FINAL);
    hex("secretstream_final_tag_header", header, sizeof header, 1);
    hex("secretstream_final_tag_ciphertext", ct, (size_t)ct_len, 0);
    sodium_free(ct);

    printf("}\n");
    return 0;
}
