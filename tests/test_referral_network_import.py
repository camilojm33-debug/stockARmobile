def test_referral_network_service_imports():
    import services.referral_network_service as service

    assert service.current_user is not None
